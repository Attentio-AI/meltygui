#pragma once
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <QuartzCore/CAMetalLayer.h>

// Integration seam, not an implementation of Melty's existing GL renderer.
// Link a class named MeltyMetalRenderer conforming to this protocol. All calls
// occur on the host's one render/Python thread. Python's frame callback runs
// before encodeFrame and must produce draw data for this SAME ImGui instance.
@protocol MeltyRenderer <NSObject>
- (instancetype)initWithDevice:(id<MTLDevice>)device error:(NSError **)error;
- (BOOL)encodeFrame:(id<MTLCommandBuffer>)commandBuffer
          drawable:(id<CAMetalDrawable>)drawable
             error:(NSError **)error;
@end

// The adapter encodes only: the host owns commit/present and limits the GPU to
// one frame in flight. Retain referenced buffers/textures until GPU completion.

// MTLCommandBuffer schedules the drawable's plain present() only after its
// writes are registered. commit() alone does not establish that ordering:
// presenting immediately afterwards can hand the IOSurface to the compositor
// before the GPU owns it, leaving both queues blocked on its IOFence.
static inline void MeltyPresentFrame(id<MTLCommandBuffer> commands,
                                     id<CAMetalDrawable> drawable) {
    [commands presentDrawable:drawable];
    [commands commit];
}
