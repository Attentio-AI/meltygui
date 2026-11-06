#import <UIKit/UIKit.h>
#import "MeltyHost.h"
#if __has_include("Launcher/HubLauncher.h")
#import "Launcher/HubLauncher.h"
#endif

@interface MeltyView : UIView <UIKeyInput>
@property(nonatomic, strong) MeltyHost *host;
@property(nonatomic, readonly) CAMetalLayer *metalLayer;
@end

@implementation MeltyView {
    NSMapTable<UITouch *, NSNumber *> *_touchIDs;
    uint64_t _nextTouchID;
    UIPanGestureRecognizer *_velocityPan;
    __weak UITouch *_velocityTouch;
    CGPoint _releaseVelocity;
    BOOL _hasReleaseVelocity;
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
        // Observe UIKit's filtered velocity without taking over touch delivery.
        _velocityPan = [[UIPanGestureRecognizer alloc] initWithTarget:self action:@selector(captureReleaseVelocity:)];
        _velocityPan.maximumNumberOfTouches = 1;
        _velocityPan.cancelsTouchesInView = NO;
        _velocityPan.delaysTouchesBegan = NO;
        _velocityPan.delaysTouchesEnded = NO;
        [self addGestureRecognizer:_velocityPan];
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
- (void)captureReleaseVelocity:(UIPanGestureRecognizer *)recognizer {
    if (recognizer.state == UIGestureRecognizerStateEnded) {
        _releaseVelocity = [recognizer velocityInView:self];
        _hasReleaseVelocity = YES;
    }
}
- (void)sendTouches:(NSSet<UITouch *> *)touches event:(UIEvent *)event kind:(melty::InputKind)kind {
    std::vector<melty::InputEvent> batch;
    for (UITouch *touch in touches) {
        if (kind == melty::InputKind::Begin && _touchIDs.count == 0) {
            _velocityTouch = touch;
            _hasReleaseVelocity = NO;
        }
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
            if (kind == melty::InputKind::End && touch == _velocityTouch) {
                CGPoint velocity = CGPointZero;
                if (_hasReleaseVelocity) velocity = _releaseVelocity;
                else if (_velocityPan.state == UIGestureRecognizerStateBegan ||
                         _velocityPan.state == UIGestureRecognizerStateChanged ||
                         _velocityPan.state == UIGestureRecognizerStateEnded)
                    velocity = [_velocityPan velocityInView:self];
                input.velocityX = velocity.x;
                input.velocityY = velocity.y;
            }
            batch.push_back(std::move(input));
        }
        if (kind == melty::InputKind::End || kind == melty::InputKind::Cancel) {
            [_touchIDs removeObjectForKey:touch];
            if (touch == _velocityTouch) {
                _velocityTouch = nil;
                _hasReleaseVelocity = NO;
            }
        }
    }
    [self.host enqueueBatch:std::move(batch)];
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

@interface MeltyController : UIViewController <UITabBarDelegate>
@property(nonatomic, strong) MeltyHost *host;
@property(nonatomic, strong) NSLayoutConstraint *automaticBottom;
@property(nonatomic) BOOL runtimeFullscreen;
@end
@implementation MeltyController
- (void)tabBar:(UITabBar *)tabBar didSelectItem:(UITabBarItem *)item {
    [self.host selectWindowTab:item.tag];
}
- (BOOL)prefersStatusBarHidden { return self.runtimeFullscreen; }
- (void)viewSafeAreaInsetsDidChange {
    [super viewSafeAreaInsetsDidChange];
    [self updateBottomInset];
}
- (void)updateBottomInset {
    // Draw behind the home indicator; only explicit app reserves inset the bottom.
    self.automaticBottom.constant = -MAX(0, self.additionalSafeAreaInsets.bottom);
}
- (void)loadView {
    // UIKit owns the unobscured native viewport. Resizing this child feeds the
    // real bounds through MeltyView.layoutSubviews into Python's normal native
    // containment/layout path; it does not move individual Melty windows.
    UIView *root = [[UIView alloc] initWithFrame:UIScreen.mainScreen.bounds];
    root.backgroundColor = UIColor.blackColor;
    self.view = root;
    // UIKit owns the top/side edges; content extends behind the home indicator.
    // Manual top/bottom reserves remain available; automatic layout is the default.
    UIView *container = [[UIView alloc] initWithFrame:CGRectZero];
    container.translatesAutoresizingMaskIntoConstraints = NO;
    [root addSubview:container];
    NSLayoutConstraint *safeZoneTop = [container.topAnchor constraintEqualToAnchor:root.topAnchor];
    safeZoneTop.priority = UILayoutPriorityRequired - 1;
    NSLayoutConstraint *safeZoneBottom = [container.bottomAnchor constraintEqualToAnchor:root.bottomAnchor];
    safeZoneBottom.priority = UILayoutPriorityRequired - 1;
    NSLayoutConstraint *automaticTop = [container.topAnchor constraintEqualToAnchor:root.safeAreaLayoutGuide.topAnchor];
    NSLayoutConstraint *automaticBottom = [container.bottomAnchor constraintEqualToAnchor:root.bottomAnchor];
    self.automaticBottom = automaticBottom;
    [self updateBottomInset];
    automaticTop.priority = automaticBottom.priority = UILayoutPriorityRequired - 1;
    NSArray *safeSides = @[
        [container.leadingAnchor constraintEqualToAnchor:root.safeAreaLayoutGuide.leadingAnchor],
        [container.trailingAnchor constraintEqualToAnchor:root.safeAreaLayoutGuide.trailingAnchor],
    ];
    [NSLayoutConstraint activateConstraints:safeSides];
    __block CGFloat requestedTop = -1, requestedBottom = -1;
    __weak MeltyController *weakController = self;
    void (^applyViewport)(void) = ^{
        [NSLayoutConstraint deactivateConstraints:@[automaticTop, automaticBottom, safeZoneTop, safeZoneBottom]];
        safeZoneTop.constant = MAX(0, requestedTop);
        safeZoneBottom.constant = -MAX(0, requestedBottom);
        [NSLayoutConstraint activateConstraints:@[
            requestedTop == -1 ? automaticTop : safeZoneTop,
            requestedBottom == -1 ? automaticBottom : safeZoneBottom,
        ]];
        [weakController.view setNeedsLayout];
    };
    [NSLayoutConstraint activateConstraints:@[
        automaticTop,
        [container.topAnchor constraintGreaterThanOrEqualToAnchor:root.topAnchor],
        [container.heightAnchor constraintGreaterThanOrEqualToConstant:1],
        automaticBottom,
        [container.bottomAnchor constraintLessThanOrEqualToAnchor:root.bottomAnchor],
    ]];
    MeltyView *view = [[MeltyView alloc] initWithFrame:CGRectZero];
    view.translatesAutoresizingMaskIntoConstraints = NO;
    [container addSubview:view];
    UIKeyboardLayoutGuide *keyboard = container.keyboardLayoutGuide;
    keyboard.usesBottomSafeArea = NO; // The container already owns the bottom reserve.
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
    UITextView *status = [[UITextView alloc] initWithFrame:CGRectZero];
    status.text = @"Starting embedded Python…";
    status.textColor = UIColor.whiteColor;
    status.font = [UIFont monospacedSystemFontOfSize:14 weight:UIFontWeightRegular];
    status.backgroundColor = UIColor.blackColor;
    status.editable = NO;
    status.selectable = YES;
    status.alwaysBounceVertical = YES;
    status.textContainerInset = UIEdgeInsetsZero;
    status.textContainer.lineFragmentPadding = 0;
    status.translatesAutoresizingMaskIntoConstraints = NO;
    [view addSubview:status];
    [NSLayoutConstraint activateConstraints:@[
        [status.leadingAnchor constraintEqualToAnchor:view.safeAreaLayoutGuide.leadingAnchor constant:20],
        [status.trailingAnchor constraintEqualToAnchor:view.safeAreaLayoutGuide.trailingAnchor constant:-20],
        [status.topAnchor constraintEqualToAnchor:view.safeAreaLayoutGuide.topAnchor constant:20],
        [status.bottomAnchor constraintEqualToAnchor:view.safeAreaLayoutGuide.bottomAnchor constant:-20],
    ]];
    __weak MeltyView *weakView = view;
    self.host = [[MeltyHost alloc] initWithLayer:view.metalLayer viewport:view.layer status:^(NSString *message) {
        status.text = message;
        status.hidden = message.length == 0;
        [status setContentOffset:CGPointZero animated:NO];
    } keyboard:^(BOOL visible) {
        if (visible) [weakView becomeFirstResponder]; else [weakView resignFirstResponder];
    } safeZone:^(CGFloat top, CGFloat bottom) {
        requestedTop = top; requestedBottom = bottom;
        applyViewport();
    }];
    self.host.windowTabsChanged = ^(NSArray<NSString *> *titles) {
        MeltyController *controller = weakController;
        if (!controller || titles.count < 2) return;
        UIView *root = controller.view;
        UIView *container = weakView.superview;
        UITabBar *tabs = [[UITabBar alloc] initWithFrame:CGRectZero];
        tabs.translatesAutoresizingMaskIntoConstraints = NO;
        tabs.delegate = controller;
        tabs.translucent = NO;
        // The viewport prefers the full screen at priority 999. Preserve the
        // native bar's height so that preference cannot collapse it to zero.
        [tabs setContentCompressionResistancePriority:UILayoutPriorityRequired
                                             forAxis:UILayoutConstraintAxisVertical];
        NSMutableArray<UITabBarItem *> *items = [NSMutableArray array];
        for (NSUInteger i = 0; i < titles.count; ++i) {
            UITabBarItem *item = [[UITabBarItem alloc] initWithTitle:titles[i]
                image:[UIImage systemImageNamed:@"macwindow"] tag:i];
            [items addObject:item];
        }
        tabs.items = items;
        tabs.selectedItem = items.firstObject;
        [root addSubview:tabs];
        // Required containment wins over either automatic or manual reserves.
        // UIKit supplies the bar's intrinsic height and bottom safe-area padding.
        [NSLayoutConstraint activateConstraints:@[
            [tabs.leadingAnchor constraintEqualToAnchor:root.leadingAnchor],
            [tabs.trailingAnchor constraintEqualToAnchor:root.trailingAnchor],
            [tabs.bottomAnchor constraintEqualToAnchor:root.bottomAnchor],
            [container.bottomAnchor constraintLessThanOrEqualToAnchor:tabs.topAnchor],
        ]];
        [root setNeedsLayout];
    };
    view.host = self.host;
#if __has_include("Launcher/HubLauncher.h")
    __weak MeltyHost *weakHost = self.host;
    HubConfigureFullscreenSetting(^(BOOL full) {
        weakController.runtimeFullscreen = full;
        [weakController setNeedsStatusBarAppearanceUpdate];
        applyViewport();
    });
    HubConfigureFPSSetting(^(BOOL visible) { [weakHost setFPSVisible:visible]; });
    status.text = @"";
    HubLaunchStart(self, ^{
        status.text = @"Starting app…";
        [self.host start];
    });
#else
    [self.host start];
#endif
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
#if __has_include("Launcher/HubLauncher.h")
    HubLaunchReceiveURLs(options.URLContexts);
#endif
    self.window = [[UIWindow alloc] initWithWindowScene:(UIWindowScene *)scene];
    if (!gController) gController = [MeltyController new];
    self.controller = gController;
    self.window.rootViewController = self.controller;
    [self.window makeKeyAndVisible];
}
- (void)sceneDidBecomeActive:(UIScene *)scene {
    [self.controller.host setActive:YES];
#if __has_include("Launcher/HubLauncher.h")
    HubLaunchBecameActive();
#endif
}
#if __has_include("Launcher/HubLauncher.h")
- (void)scene:(UIScene *)scene openURLContexts:(NSSet<UIOpenURLContext *> *)contexts { HubLaunchReceiveURLs(contexts); }
#endif
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
