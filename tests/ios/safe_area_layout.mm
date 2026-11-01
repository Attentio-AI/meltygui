// Link with production Host/App.mm and InputQueue.hpp, without Python.
// Exercises real UIKit safe-area changes and the automatic/manual transition.
#import "MeltyHost.h"
#import <UIKit/UIKit.h>
#import <Metal/Metal.h>

@implementation MeltyHost {
    CALayer *_viewport;
    void (^_safeZone)(CGFloat, CGFloat);
    NSMutableArray *_checks;
}
- (instancetype)initWithLayer:(CAMetalLayer *)layer viewport:(CALayer *)viewport
                       status:(void (^)(NSString *))status keyboard:(void (^)(BOOL))keyboard
                     safeZone:(void (^)(CGFloat, CGFloat))safeZone {
    if ((self = [super init])) {
        layer.device = MTLCreateSystemDefaultDevice();
        _viewport = viewport;
        _safeZone = [safeZone copy];
        _checks = [NSMutableArray array];
        status(@"");
    }
    return self;
}
- (void)record:(NSString *)name manual:(BOOL)manual {
    UIView *canvas = (UIView *)_viewport.delegate;
    UIViewController *controller = canvas.window.rootViewController;
    UIView *root = controller.view;
    [root setNeedsLayout];
    [root layoutIfNeeded];
    CGRect actual = [canvas convertRect:canvas.bounds toView:root];
    CGRect expected = root.safeAreaLayoutGuide.layoutFrame;
    CGFloat extra = MAX(0, controller.additionalSafeAreaInsets.bottom);
    expected.size.height = root.bounds.size.height - extra - expected.origin.y;
    if (manual) {
        expected.origin.y = 64;
        expected.size.height = root.bounds.size.height - 64 - 28;
    }
    BOOL passed = fabs(actual.origin.x - expected.origin.x) < .01 &&
                  fabs(actual.origin.y - expected.origin.y) < .01 &&
                  fabs(actual.size.width - expected.size.width) < .01 &&
                  fabs(actual.size.height - expected.size.height) < .01;
    [_checks addObject:@{@"case":name, @"passed":@(passed),
        @"actual":NSStringFromCGRect(actual), @"expected":NSStringFromCGRect(expected)}];
}
- (void)start {
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC), dispatch_get_main_queue(), ^{
        UIView *canvas = (UIView *)self->_viewport.delegate;
        UIViewController *controller = canvas.window.rootViewController;
        [self record:@"initial device safe area" manual:NO];
        controller.additionalSafeAreaInsets = UIEdgeInsetsMake(17, 13, 19, 11);
        [self record:@"changed safe area on all four edges" manual:NO];
        self->_safeZone(64, 28);
        [self record:@"manual reserves preserve safe side edges" manual:YES];
        self->_safeZone(-1, -1);
        [self record:@"automatic anchors restored" manual:NO];
        controller.additionalSafeAreaInsets = UIEdgeInsetsZero;
        [self record:@"additional insets removed without leftover padding" manual:NO];
        UIWindowScene *scene = canvas.window.windowScene;
        UIInterfaceOrientationMask orientation = UIInterfaceOrientationIsPortrait(scene.interfaceOrientation)
            ? UIInterfaceOrientationMaskLandscapeRight : UIInterfaceOrientationMaskPortrait;
        __block BOOL rotationUnavailable = NO;
        [controller setNeedsUpdateOfSupportedInterfaceOrientations];
        [scene requestGeometryUpdateWithPreferences:
            [[UIWindowSceneGeometryPreferencesIOS alloc] initWithInterfaceOrientations:orientation]
            errorHandler:^(NSError *error) {
                rotationUnavailable = YES;
                [self->_checks addObject:@{@"case":@"rotation request", @"skipped":@YES,
                                           @"reason":error.localizedDescription}];
            }];
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC), dispatch_get_main_queue(), ^{
            if (!rotationUnavailable) {
                BOOL rotated = orientation == UIInterfaceOrientationMaskPortrait
                    ? UIInterfaceOrientationIsPortrait(scene.interfaceOrientation)
                    : UIInterfaceOrientationIsLandscape(scene.interfaceOrientation);
                [self->_checks addObject:@{@"case":@"rotation applied", @"passed":@(rotated)}];
                [self record:@"safe area after rotation" manual:NO];
            }
            BOOL passed = YES;
            for (NSDictionary *check in self->_checks)
                if (![check[@"skipped"] boolValue]) passed &= [check[@"passed"] boolValue];
            NSData *data = [NSJSONSerialization dataWithJSONObject:
                @{@"passed":@(passed), @"checks":self->_checks}
                options:NSJSONWritingPrettyPrinted error:nil];
            NSURL *documents = [NSFileManager.defaultManager URLsForDirectory:NSDocumentDirectory
                                                                    inDomains:NSUserDomainMask].firstObject;
            [data writeToURL:[documents URLByAppendingPathComponent:@"safe-area-results.json"] atomically:YES];
        });
    });
}
- (void)setActive:(BOOL)active {}
- (void)close {}
- (void)resizeTo:(CGSize)size scale:(CGFloat)scale {}
- (void)enqueue:(melty::InputEvent)event {}
- (void)requestFrame {}
- (void)setKeyboardVisible:(BOOL)visible {}
- (void)setSafeZone:(CGFloat)top bottom:(CGFloat)bottom { _safeZone(top, bottom); }
- (NSString *)clipboardText { return @""; }
- (void)setClipboardText:(NSString *)text {}
- (void)writeLog:(NSString *)text {}
@end
