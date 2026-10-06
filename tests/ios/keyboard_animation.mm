// UIKit integration probe. Link the production App.mm with this small host,
// without Python, to exercise real keyboard animations in an ARM64 simulator.
// The Python renderer/final presentation pass have separate Metal pixel tests.
#import "MeltyHost.h"
#import <UIKit/UIKit.h>
#import <Metal/Metal.h>

@implementation MeltyHost {
    CAMetalLayer *_layer;
    CALayer *_viewport;
    void (^_keyboard)(BOOL);
    void (^_safeZone)(CGFloat, CGFloat);
    void (^_status)(NSString *);
    CADisplayLink *_link;
    id<MTLCommandQueue> _queue;
    id<MTLRenderPipelineState> _pipeline;
    NSMutableArray *_samples;
    double _started;
    CGFloat _scale;
    int _phase;
}
- (instancetype)initWithLayer:(CAMetalLayer *)layer viewport:(CALayer *)viewport
                       status:(void (^)(NSString *))status keyboard:(void (^)(BOOL))keyboard
                     safeZone:(void (^)(CGFloat, CGFloat))safeZone {
    if ((self = [super init])) {
        _layer = layer; _viewport = viewport;
        _status = [status copy]; _keyboard = [keyboard copy]; _safeZone = [safeZone copy];
        _layer.device = MTLCreateSystemDefaultDevice();
        _layer.pixelFormat = MTLPixelFormatBGRA8Unorm;
        _queue = [_layer.device newCommandQueue];
        NSError *error = nil;
        NSString *shader = @"#include <metal_stdlib>\nusing namespace metal;\n"
            "vertex float4 v(uint i [[vertex_id]]) { float2 p[3]={float2(-1,-1),float2(3,-1),float2(-1,3)}; return float4(p[i],0,1); }\n"
            "fragment float4 f(float4 p [[position]]) { uint2 cell=uint2(p.xy)/60; return ((cell.x+cell.y)%2) ? float4(.15,.3,.45,1) : float4(.8,.8,.8,1); }";
        id<MTLLibrary> library = [_layer.device newLibraryWithSource:shader options:nil error:&error];
        NSAssert(library, @"%@", error);
        MTLRenderPipelineDescriptor *descriptor = [MTLRenderPipelineDescriptor new];
        descriptor.vertexFunction = [library newFunctionWithName:@"v"];
        descriptor.fragmentFunction = [library newFunctionWithName:@"f"];
        descriptor.colorAttachments[0].pixelFormat = _layer.pixelFormat;
        _pipeline = [_layer.device newRenderPipelineStateWithDescriptor:descriptor error:&error];
        NSAssert(_pipeline, @"%@", error);
        _samples = [NSMutableArray new];
    }
    return self;
}
- (void)start {
    _status(@""); _safeZone(64, 24);
    _link = [CADisplayLink displayLinkWithTarget:self selector:@selector(tick:)];
    _link.preferredFrameRateRange = CAFrameRateRangeMake(30,120,120);
    [_link addToRunLoop:NSRunLoop.mainRunLoop forMode:NSRunLoopCommonModes];
}
- (void)tick:(CADisplayLink *)link {
    if (!_started) _started = CACurrentMediaTime();
    double elapsed = CACurrentMediaTime() - _started;
    // Complete show/hide, followed by a show interrupted by dismissal.
    if (_phase == 0 && elapsed > 1) { _phase = 1; _keyboard(YES); }
    if (_phase == 1 && elapsed > 2.5) { _phase = 2; _keyboard(NO); }
    if (_phase == 2 && elapsed > 3.5) { _phase = 3; _keyboard(YES); }
    if (_phase == 3 && elapsed > 3.65) { _phase = 4; _keyboard(NO); }
    if (elapsed > 5) {
        [link invalidate];
        NSData *result = [NSJSONSerialization dataWithJSONObject:_samples options:NSJSONWritingPrettyPrinted error:nil];
        [result writeToFile:[NSHomeDirectory() stringByAppendingPathComponent:@"Documents/keyboard-geometry.json"] atomically:YES];
        return;
    }
    CGSize viewport = _viewport.bounds.size;
    CGSize visible = (_viewport.presentationLayer ?: _viewport).bounds.size;
    CGSize canvas = _layer.bounds.size;
    CGSize presented = (_layer.presentationLayer ?: _layer).bounds.size;
    UIView *view = (UIView *)_viewport.delegate;
    CALayer *visibleLayer = _viewport.presentationLayer ?: _viewport;
    CGPoint windowPoint = [visibleLayer convertPoint:CGPointMake(30,MAX(1,visible.height-5))
                                            toLayer:view.window.layer.presentationLayer ?: view.window.layer];
    CGPoint modelPoint = [view convertPoint:windowPoint fromView:view.window];
    BOOL visibleEdgeReceivesTouch = [view pointInside:modelPoint withEvent:nil];
    id<CAMetalDrawable> drawable = [_layer nextDrawable];
    if (!drawable) return;
    [_samples addObject:@{ @"time": @(elapsed), @"phase": @(_phase),
        @"target_height": @(viewport.height), @"visible_height": @(visible.height),
        @"canvas_height": @(canvas.height), @"presented_height": @(presented.height),
        @"drawable_height": @(drawable.texture.height), @"scale": @(_scale),
        @"visible_edge_receives_touch": @(visibleEdgeReceivesTouch) }];
    MTLRenderPassDescriptor *pass = [MTLRenderPassDescriptor renderPassDescriptor];
    pass.colorAttachments[0].texture = drawable.texture;
    pass.colorAttachments[0].loadAction = MTLLoadActionDontCare;
    pass.colorAttachments[0].storeAction = MTLStoreActionStore;
    id<MTLCommandBuffer> commands = [_queue commandBuffer];
    id<MTLRenderCommandEncoder> encoder = [commands renderCommandEncoderWithDescriptor:pass];
    [encoder setRenderPipelineState:_pipeline];
    [encoder drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
    [encoder endEncoding];
    [commands presentDrawable:drawable];
    [commands commit];
}
- (void)resizeTo:(CGSize)size scale:(CGFloat)scale {
    _scale = scale;
    CGSize canvas = _layer.bounds.size;
    if (canvas.width <= 0 || canvas.height <= 0) return;
    [CATransaction begin]; [CATransaction setDisableActions:YES];
    _layer.drawableSize = CGSizeMake(round(canvas.width*scale), round(canvas.height*scale));
    [CATransaction commit];
}
- (void)setActive:(BOOL)active { _link.paused = !active; }
- (void)close { [_link invalidate]; }
- (void)enqueue:(melty::InputEvent)event {}
- (void)requestFrame {}
- (void)setKeyboardVisible:(BOOL)visible { _keyboard(visible); }
- (void)setSafeZone:(CGFloat)top bottom:(CGFloat)bottom { _safeZone(top, bottom); }
- (NSString *)clipboardText { return @""; }
- (void)setClipboardText:(NSString *)text {}
- (void)writeLog:(NSString *)text { NSLog(@"%@", text); }
@end
