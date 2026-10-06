// Exercise the production submission helper with real display-link drawables.
// Offscreen texture tests cannot catch an IOSurface presentation fence stall.
#import <Cocoa/Cocoa.h>
#import <QuartzCore/CAMetalDisplayLink.h>
#import "MeltyRenderer.h"

@interface PresentationProbe : NSObject <CAMetalDisplayLinkDelegate>
@property(nonatomic, strong) id<MTLCommandQueue> queue;
@property(nonatomic) NSUInteger submitted;
@property(nonatomic) NSUInteger presented;
@property(nonatomic) NSUInteger completed;
@property(nonatomic, copy) NSString *failure;
@end

@implementation PresentationProbe
- (void)metalDisplayLink:(CAMetalDisplayLink *)link needsUpdate:(CAMetalDisplayLinkUpdate *)update {
    if (self.submitted >= 90 || self.failure) { link.paused = YES; return; }
    id<MTLCommandBuffer> commands = [self.queue commandBuffer];
    MTLRenderPassDescriptor *pass = [MTLRenderPassDescriptor renderPassDescriptor];
    pass.colorAttachments[0].texture = update.drawable.texture;
    pass.colorAttachments[0].loadAction = MTLLoadActionClear;
    pass.colorAttachments[0].storeAction = MTLStoreActionStore;
    pass.colorAttachments[0].clearColor = MTLClearColorMake((self.submitted % 3) / 3.0, .2, .3, 1);
    [[commands renderCommandEncoderWithDescriptor:pass] endEncoding];
    [commands addCompletedHandler:^(id<MTLCommandBuffer> done) {
        dispatch_async(dispatch_get_main_queue(), ^{
            self.completed++;
            if (done.error) self.failure = done.error.description;
        });
    }];
    [update.drawable addPresentedHandler:^(id<MTLDrawable> drawable) {
        dispatch_async(dispatch_get_main_queue(), ^{ self.presented++; });
    }];
    MeltyPresentFrame(commands, update.drawable);
    self.submitted++;
}
@end

int main() {
    @autoreleasepool {
        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyProhibited];
        NSWindow *window = [[NSWindow alloc] initWithContentRect:NSMakeRect(40,40,160,120)
            styleMask:NSWindowStyleMaskBorderless backing:NSBackingStoreBuffered defer:NO];
        CAMetalLayer *layer = [CAMetalLayer layer];
        layer.device = MTLCreateSystemDefaultDevice();
        layer.pixelFormat = MTLPixelFormatRGBA16Float;
        layer.framebufferOnly = YES;
        layer.maximumDrawableCount = 3;
        layer.drawableSize = CGSizeMake(160,120);
        window.contentView.wantsLayer = YES;
        window.contentView.layer = layer;
        [window orderBack:nil];
        PresentationProbe *probe = [PresentationProbe new];
        probe.queue = [layer.device newCommandQueue];
        CAMetalDisplayLink *link = [[CAMetalDisplayLink alloc] initWithMetalLayer:layer];
        link.delegate = probe;
        link.preferredFrameRateRange = CAFrameRateRangeMake(30,120,60);
        [link addToRunLoop:NSRunLoop.mainRunLoop forMode:NSRunLoopCommonModes];
        NSDate *limit = [NSDate dateWithTimeIntervalSinceNow:10];
        while ((probe.presented < 90 || probe.completed < 90) && !probe.failure && limit.timeIntervalSinceNow > 0) {
            [NSRunLoop.mainRunLoop runMode:NSDefaultRunLoopMode beforeDate:[NSDate dateWithTimeIntervalSinceNow:.01]];
        }
        [link invalidate];
        [window orderOut:nil];
        printf("submitted=%lu completed=%lu presented=%lu\n", probe.submitted, probe.completed, probe.presented);
        if (probe.failure) fprintf(stderr,"%s\n", probe.failure.UTF8String);
        return probe.failure || probe.completed != 90 || probe.presented != 90;
    }
}
