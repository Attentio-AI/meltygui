#import <UIKit/UIKit.h>
#import "MeltyHost.h"

@interface MeltyView : UIView <UIKeyInput>
@property(nonatomic, strong) MeltyHost *host;
@property(nonatomic, readonly) CAMetalLayer *metalLayer;
@end

@implementation MeltyView {
    NSMapTable<UITouch *, NSNumber *> *_touchIDs;
    uint64_t _nextTouchID;
    CAMetalLayer *_metalLayer;
}
- (instancetype)initWithFrame:(CGRect)frame {
    if ((self = [super initWithFrame:frame])) {
        self.multipleTouchEnabled = YES;
        self.backgroundColor = UIColor.blackColor;
        // UIKit animates this view's clipping bounds with the keyboard. Its
        // Metal sublayer keeps a full-size, unanimated canvas: changing the
        // clip must never rescale the last presented drawable.
        self.clipsToBounds = YES;
        _metalLayer = [CAMetalLayer layer];
        _metalLayer.anchorPoint = CGPointZero;
        [self.layer addSublayer:_metalLayer];
        _touchIDs = [NSMapTable weakToStrongObjectsMapTable];
    }
    return self;
}
- (CAMetalLayer *)metalLayer { return _metalLayer; }
- (BOOL)canBecomeFirstResponder { return YES; }
- (BOOL)hasText { return YES; }
- (UIKeyboardType)keyboardType { return UIKeyboardTypeDefault; }
- (UITextAutocorrectionType)autocorrectionType { return UITextAutocorrectionTypeNo; }
- (UITextAutocapitalizationType)autocapitalizationType { return UITextAutocapitalizationTypeNone; }
- (UITextSmartQuotesType)smartQuotesType { return UITextSmartQuotesTypeNo; }
- (UITextSmartDashesType)smartDashesType { return UITextSmartDashesTypeNo; }
- (void)insertText:(NSString *)text {
    melty::InputEvent event{melty::InputKind::Text, 0, CACurrentMediaTime()};
    event.text = text.UTF8String;
    [self.host enqueue:std::move(event)];
}
- (void)deleteBackward {
    [self.host enqueue:melty::InputEvent{melty::InputKind::Backspace, 0, CACurrentMediaTime()}];
}
- (void)layoutSubviews {
    [super layoutSubviews];
    CGFloat scale = self.window.screen.scale ?: UIScreen.mainScreen.scale;
    self.contentScaleFactor = scale;
    CGSize canvas = self.superview ? self.superview.bounds.size : self.bounds.size;
    [CATransaction begin];
    [CATransaction setDisableActions:YES];
    _metalLayer.contentsScale = scale;
    _metalLayer.position = CGPointZero;
    _metalLayer.bounds = (CGRect){CGPointZero, canvas};
    [CATransaction commit];
    [self.host resizeTo:self.bounds.size scale:scale];
}
- (CGPoint)visiblePointFromWindow:(CGPoint)point {
    CALayer *visible = self.layer.presentationLayer ?: self.layer;
    return [visible convertPoint:point
                       fromLayer:self.window.layer.presentationLayer ?: self.window.layer];
}
- (BOOL)pointInside:(CGPoint)point withEvent:(UIEvent *)event {
    CALayer *visible = self.layer.presentationLayer;
    if (!visible || !self.window) return [super pointInside:point withEvent:event];
    // The still-visible strip can lie outside the final model bounds while
    // the keyboard is rising. Keep that strip interactive until it is clipped.
    CGPoint windowPoint = [self convertPoint:point toView:self.window];
    return CGRectContainsPoint(visible.bounds, [self visiblePointFromWindow:windowPoint]);
}
- (void)sendTouches:(NSSet<UITouch *> *)touches event:(UIEvent *)event kind:(melty::InputKind)kind {
    for (UITouch *touch in touches) {
        NSNumber *identity = [_touchIDs objectForKey:touch];
        if (!identity) {
            identity = @(++_nextTouchID);
            [_touchIDs setObject:identity forKey:touch];
        }
        NSArray<UITouch *> *samples = kind == melty::InputKind::Move
            ? ([event coalescedTouchesForTouch:touch] ?: @[touch]) : @[touch];
        for (UITouch *sample in samples) {
            // During an animated top-edge move, UIKit's model coordinates
            // already describe the destination. Hit-test the visible canvas.
            CGPoint point = self.window
                ? [self visiblePointFromWindow:[sample locationInView:self.window]]
                : [sample locationInView:self];
            melty::InputEvent input{kind, identity.unsignedLongLongValue, sample.timestamp};
            input.x = point.x;
            input.y = point.y;
            input.pressure = sample.maximumPossibleForce > 0 ? sample.force / sample.maximumPossibleForce : 0;
            input.pencil = sample.type == UITouchTypePencil;
            [self.host enqueue:std::move(input)];
        }
        if (kind == melty::InputKind::End || kind == melty::InputKind::Cancel) [_touchIDs removeObjectForKey:touch];
    }
}
- (void)touchesBegan:(NSSet<UITouch *> *)touches withEvent:(UIEvent *)event {
    [self sendTouches:touches event:event kind:melty::InputKind::Begin];
}
- (void)touchesMoved:(NSSet<UITouch *> *)touches withEvent:(UIEvent *)event {
    [self sendTouches:touches event:event kind:melty::InputKind::Move];
}
- (void)touchesEnded:(NSSet<UITouch *> *)touches withEvent:(UIEvent *)event {
    [self sendTouches:touches event:event kind:melty::InputKind::End];
}
- (void)touchesCancelled:(NSSet<UITouch *> *)touches withEvent:(UIEvent *)event {
    [self sendTouches:touches event:event kind:melty::InputKind::Cancel];
}
@end

@interface MeltyController : UIViewController
@property(nonatomic, strong) MeltyHost *host;
@end
@implementation MeltyController
- (void)loadView {
    // UIKit owns the unobscured native viewport. Resizing this child feeds the
    // real bounds through MeltyView.layoutSubviews into Python's normal native
    // containment/layout path; it does not move individual Melty windows.
    UIView *root = [[UIView alloc] initWithFrame:UIScreen.mainScreen.bounds];
    root.backgroundColor = UIColor.blackColor;
    self.view = root;
    // The configurable top strip belongs to UIKit, outside the Metal canvas.
    // Keyboard geometry and touches then use the same local content coordinates.
    UIView *container = [[UIView alloc] initWithFrame:CGRectZero];
    container.translatesAutoresizingMaskIntoConstraints = NO;
    [root addSubview:container];
    NSLayoutConstraint *safeZoneTop = [container.topAnchor constraintEqualToAnchor:root.topAnchor];
    safeZoneTop.priority = UILayoutPriorityRequired - 1;
    [NSLayoutConstraint activateConstraints:@[
        safeZoneTop,
        [container.topAnchor constraintGreaterThanOrEqualToAnchor:root.topAnchor],
        [container.heightAnchor constraintGreaterThanOrEqualToConstant:1],
        [container.leadingAnchor constraintEqualToAnchor:root.leadingAnchor],
        [container.trailingAnchor constraintEqualToAnchor:root.trailingAnchor],
        [container.bottomAnchor constraintEqualToAnchor:root.bottomAnchor],
    ]];
    MeltyView *view = [[MeltyView alloc] initWithFrame:CGRectZero];
    view.translatesAutoresizingMaskIntoConstraints = NO;
    [container addSubview:view];
    UIKeyboardLayoutGuide *keyboard = container.keyboardLayoutGuide;
    keyboard.usesBottomSafeArea = NO; // Restore the original full viewport when hidden.
    keyboard.followsUndockedKeyboard = YES;
    [NSLayoutConstraint activateConstraints:@[
        [view.leadingAnchor constraintEqualToAnchor:container.leadingAnchor],
        [view.trailingAnchor constraintEqualToAnchor:container.trailingAnchor],
    ]];
    // The guide already converts/intersects keyboard geometry for this scene
    // and tracks rotation, hardware-keyboard toolbars and dismissal animations.
    // A floating keyboard near the top leaves the usable viewport BELOW it;
    // otherwise keep the canvas above it (also the docked/hidden behavior).
    [keyboard setConstraints:@[
        [view.topAnchor constraintEqualToAnchor:container.topAnchor],
        [view.bottomAnchor constraintEqualToAnchor:keyboard.topAnchor],
    ] activeWhenAwayFromEdge:NSDirectionalRectEdgeTop];
    [keyboard setConstraints:@[
        [view.topAnchor constraintEqualToAnchor:keyboard.bottomAnchor],
        [view.bottomAnchor constraintEqualToAnchor:container.bottomAnchor],
    ] activeWhenNearEdge:NSDirectionalRectEdgeTop];
    // Touch coordinates remain local to the Metal view via locationInView:;
    // no keyboard offset is added to the native input or Python geometry.
    UILabel *status = [[UILabel alloc] initWithFrame:CGRectZero];
    status.text = @"Starting embedded Python…";
    status.textColor = UIColor.whiteColor;
    status.font = [UIFont monospacedSystemFontOfSize:14 weight:UIFontWeightRegular];
    status.numberOfLines = 0;
    status.translatesAutoresizingMaskIntoConstraints = NO;
    status.userInteractionEnabled = NO;
    [view addSubview:status];
    [NSLayoutConstraint activateConstraints:@[
        [status.leadingAnchor constraintEqualToAnchor:view.safeAreaLayoutGuide.leadingAnchor constant:20],
        [status.trailingAnchor constraintEqualToAnchor:view.safeAreaLayoutGuide.trailingAnchor constant:-20],
        [status.topAnchor constraintEqualToAnchor:view.safeAreaLayoutGuide.topAnchor constant:20],
        [status.bottomAnchor constraintLessThanOrEqualToAnchor:view.safeAreaLayoutGuide.bottomAnchor constant:-20],
    ]];
    __weak MeltyView *weakView = view;
    __weak UIView *weakRoot = root;
    self.host = [[MeltyHost alloc] initWithLayer:view.metalLayer viewport:view.layer status:^(NSString *message) {
        status.text = message;
        status.hidden = message.length == 0;
    } keyboard:^(BOOL visible) {
        if (visible) [weakView becomeFirstResponder]; else [weakView resignFirstResponder];
    } safeZone:^(CGFloat inset) {
        safeZoneTop.constant = inset;
        [weakRoot setNeedsLayout];
    }];
    view.host = self.host;
    [self.host start];
}
@end

// One interpreter for the process, including a scene disconnect/reconnection.
static MeltyController *gController;

@interface MeltySceneDelegate : UIResponder <UIWindowSceneDelegate>
@property(nonatomic, strong) UIWindow *window;
@property(nonatomic, strong) MeltyController *controller;
@end
@implementation MeltySceneDelegate
- (void)scene:(UIScene *)scene willConnectToSession:(UISceneSession *)session options:(UISceneConnectionOptions *)options {
    self.window = [[UIWindow alloc] initWithWindowScene:(UIWindowScene *)scene];
    if (!gController) gController = [MeltyController new];
    self.controller = gController;
    self.window.rootViewController = self.controller;
    [self.window makeKeyAndVisible];
}
- (void)sceneDidBecomeActive:(UIScene *)scene { [self.controller.host setActive:YES]; }
- (void)sceneWillResignActive:(UIScene *)scene { [self.controller.host setActive:NO]; }
- (void)sceneDidDisconnect:(UIScene *)scene { [self.controller.host setActive:NO]; }
@end

@interface MeltyAppDelegate : UIResponder <UIApplicationDelegate>
@end
@implementation MeltyAppDelegate
- (void)applicationWillTerminate:(UIApplication *)application { [gController.host close]; }
- (UISceneConfiguration *)application:(UIApplication *)application
    configurationForConnectingSceneSession:(UISceneSession *)session options:(UISceneConnectionOptions *)options {
    UISceneConfiguration *configuration = [[UISceneConfiguration alloc] initWithName:@"Melty" sessionRole:session.role];
    configuration.delegateClass = MeltySceneDelegate.class;
    return configuration;
}
@end

int main(int argc, char *argv[]) {
    @autoreleasepool {
        return UIApplicationMain(argc, argv, nil, NSStringFromClass(MeltyAppDelegate.class));
    }
}
