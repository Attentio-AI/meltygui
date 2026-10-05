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
