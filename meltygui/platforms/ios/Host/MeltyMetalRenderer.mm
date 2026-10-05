#import "MeltyRenderer.h"
#include <Python.h>
#include <vector>

typedef void (^MetalOperation)(id<MTLCommandBuffer>);
@interface MeltyMetalRenderer : NSObject <MeltyRenderer>
@property(nonatomic, strong) id<MTLDevice> device;
@property(nonatomic, strong) NSThread *owner;
@property(nonatomic, strong) id<MTLLibrary> library;
@property(nonatomic, strong) NSMutableDictionary<NSNumber *, id<MTLTexture>> *textures;
@property(nonatomic, strong) NSMutableDictionary<NSString *, id<MTLRenderPipelineState>> *pipelines;
@property(nonatomic, strong) NSMutableArray<MetalOperation> *pending;
@property(nonatomic) uint64_t nextTexture;
@property(nonatomic) uint64_t scene;
- (id<MTLRenderPipelineState>)pipeline:(NSString *)fragment format:(MTLPixelFormat)format blend:(NSString *)blend;
- (BOOL)encodeFrame:(id<MTLCommandBuffer>)commands target:(id<MTLTexture>)target error:(NSError **)error;
@end
static MeltyMetalRenderer *engine;
#if MELTY_METAL_TESTING
static NSString *testManifest;
#endif

@implementation MeltyMetalRenderer
- (instancetype)initWithDevice:(id<MTLDevice>)device error:(NSError **)error {
    if ((self = [super init])) {
        self.device=device; self.owner=NSThread.currentThread;
        NSURL *url=[NSBundle.mainBundle URLForResource:@"Melty" withExtension:@"metallib"];
        if (!url) {
            if(error) *error=[NSError errorWithDomain:@"MeltyMetal" code:1 userInfo:@{NSLocalizedDescriptionKey:@"Melty.metallib is missing; run the iOS shader packaging build phase"}];
            return nil;
        }
        self.library=[device newLibraryWithURL:url error:error];
        if(!self.library) return nil;
        self.textures=[NSMutableDictionary new]; self.pipelines=[NSMutableDictionary new]; self.pending=[NSMutableArray new];
        engine=self;
    }
    return self;
}
- (id<MTLRenderPipelineState>)pipeline:(NSString *)fragment format:(MTLPixelFormat)format blend:(NSString *)blend {
    NSString *key=[NSString stringWithFormat:@"%@:%lu:%@",fragment,(unsigned long)format,blend];
    id<MTLRenderPipelineState> cached=self.pipelines[key]; if(cached) return cached;
    MTLRenderPipelineDescriptor *d=[MTLRenderPipelineDescriptor new];
    NSString *vertex=[fragment isEqualToString:@"mesh_fragment"] ? @"mesh_vertex" :
        [fragment isEqualToString:@"shadow_shape_fragment"] ? @"shadow_shape_vertex" : @"quad_vertex";
    d.vertexFunction=[self.library newFunctionWithName:vertex];
    d.fragmentFunction=[self.library newFunctionWithName:fragment];
    if(!d.fragmentFunction) @throw [NSException exceptionWithName:@"MissingMetalShader" reason:fragment userInfo:nil];
    MTLRenderPipelineColorAttachmentDescriptor *a=d.colorAttachments[0]; a.pixelFormat=format;
    if(![blend isEqualToString:@"replace"]) {
        a.blendingEnabled=YES;
        a.sourceRGBBlendFactor=[blend isEqualToString:@"over"] ? MTLBlendFactorSourceAlpha : MTLBlendFactorOne;
        a.destinationRGBBlendFactor=[blend isEqualToString:@"over"] ? MTLBlendFactorOneMinusSourceAlpha : MTLBlendFactorOne;
        a.sourceAlphaBlendFactor=MTLBlendFactorOne;
        a.destinationAlphaBlendFactor=[blend isEqualToString:@"over"] ? MTLBlendFactorOneMinusSourceAlpha : MTLBlendFactorOne;
        a.rgbBlendOperation=[blend isEqualToString:@"max"] ? MTLBlendOperationMax : [blend isEqualToString:@"min"] ? MTLBlendOperationMin : MTLBlendOperationAdd;
        a.alphaBlendOperation=[blend isEqualToString:@"glow"] ? MTLBlendOperationMax : a.rgbBlendOperation;
    }
    NSError *error=nil; cached=[self.device newRenderPipelineStateWithDescriptor:d error:&error];
    if(!cached) @throw [NSException exceptionWithName:@"MetalPipeline" reason:error.localizedDescription userInfo:nil];
    self.pipelines[key]=cached; return cached;
}
- (BOOL)encodeFrame:(id<MTLCommandBuffer>)commands drawable:(id<CAMetalDrawable>)drawable error:(NSError **)error {
    return [self encodeFrame:commands target:drawable.texture error:error];
}
- (BOOL)encodeFrame:(id<MTLCommandBuffer>)commands target:(id<MTLTexture>)target error:(NSError **)error {
    @try {
        NSArray<MetalOperation> *operations=[self.pending copy]; [self.pending removeAllObjects];
        for(MetalOperation operation in operations) operation(commands);
        id<MTLTexture> scene=self.textures[@(self.scene)];
        if(!scene) @throw [NSException exceptionWithName:@"MetalScene" reason:@"Python did not submit a completed Metal scene" userInfo:nil];
        MTLRenderPassDescriptor *pass=[MTLRenderPassDescriptor renderPassDescriptor];
        pass.colorAttachments[0].texture=target;
        pass.colorAttachments[0].loadAction=MTLLoadActionClear; pass.colorAttachments[0].storeAction=MTLStoreActionStore;
        pass.colorAttachments[0].clearColor=MTLClearColorMake(0,0,0,1);
        id<MTLRenderCommandEncoder> encoder=[commands renderCommandEncoderWithDescriptor:pass];
        // The keyboard animates a clip over a stable full-size drawable.
        // Copy the current scene at native pixel scale, anchored at top-left.
        // This also handles a drawable acquired before a rotation/resize.
        float quad[12]={0,(float)target.height-(float)scene.height,(float)scene.width,(float)scene.height,
                       (float)target.width,(float)target.height,0,0, 1,1,0,0};
        [encoder setScissorRect:MTLScissorRect{0,0,MIN(scene.width,target.width),MIN(scene.height,target.height)}];
        NSString *present=target.pixelFormat==MTLPixelFormatRGBA16Float ? @"present_linear_fragment" : @"present_fragment";
        [encoder setRenderPipelineState:[self pipeline:present format:target.pixelFormat blend:@"replace"]];
        [encoder setVertexBytes:quad length:sizeof(quad) atIndex:0];
        [encoder setFragmentTexture:scene atIndex:0];
        [encoder drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3]; [encoder endEncoding];
        return YES;
    } @catch(NSException *exception) {
        [self.pending removeAllObjects];
        if(error) *error=[NSError errorWithDomain:@"MeltyMetal" code:2 userInfo:@{NSLocalizedDescriptionKey:exception.reason ?: exception.name}];
        return NO;
    }
}
@end

static bool ready() {
    if(!engine) { PyErr_SetString(PyExc_RuntimeError,"Metal device has not been initialized by the native host"); return false; }
    if(engine.owner!=NSThread.currentThread) { PyErr_SetString(PyExc_RuntimeError,"Metal resources belong to the native render thread"); return false; }
    return true;
}
static id<MTLTexture> texture(uint64_t handle) {
    id<MTLTexture> t=engine.textures[@(handle)];
    if(!t) PyErr_Format(PyExc_ValueError,"Unknown Metal texture handle %llu",(unsigned long long)handle);
    return t;
}
static NSData *bytes(PyObject *object) {
    Py_buffer view; if(PyObject_GetBuffer(object,&view,PyBUF_CONTIG_RO)) return nil;
    NSData *result=[NSData dataWithBytes:view.buf length:(NSUInteger)view.len]; PyBuffer_Release(&view); return result;
}
static bool numbers(PyObject *object,float *values,Py_ssize_t count) {
    PyObject *s=PySequence_Fast(object,"Expected numeric sequence"); if(!s) return false;
    if(PySequence_Fast_GET_SIZE(s)!=count) { Py_DECREF(s); PyErr_SetString(PyExc_ValueError,"Wrong numeric sequence length"); return false; }
    for(Py_ssize_t i=0;i<count;i++) { values[i]=(float)PyFloat_AsDouble(PySequence_Fast_GET_ITEM(s,i)); if(PyErr_Occurred()) { Py_DECREF(s); return false; } }
    Py_DECREF(s); return true;
}
static MTLRenderPassDescriptor *passFor(id<MTLTexture> target) {
    MTLRenderPassDescriptor *p=[MTLRenderPassDescriptor renderPassDescriptor];
    p.colorAttachments[0].texture=target; p.colorAttachments[0].loadAction=MTLLoadActionLoad; p.colorAttachments[0].storeAction=MTLStoreActionStore; return p;
}
static PyObject *createTexture(PyObject *,PyObject *args) {
    int w,h; const char *format; PyObject *pixels=Py_None;
    if(!PyArg_ParseTuple(args,"iis|O",&w,&h,&format,&pixels)||!ready()) return nullptr;
    if(w<1 || h<1 || w>16384 || h>16384) { PyErr_SetString(PyExc_ValueError,"Texture dimensions must be 1..16384"); return nullptr; }
    MTLPixelFormat f; size_t stride;
    if(!strcmp(format,"rgba16f")) { f=MTLPixelFormatRGBA16Float; stride=8; }
    else if(!strcmp(format,"rgba32f")) { f=MTLPixelFormatRGBA32Float; stride=16; }
    else if(!strcmp(format,"r16")) { f=MTLPixelFormatR16Unorm; stride=2; }
    else if(!strcmp(format,"rgba8")) { f=MTLPixelFormatRGBA8Unorm; stride=4; }
    else if(!strcmp(format,"rgba8srgb")) { f=MTLPixelFormatRGBA8Unorm_sRGB; stride=4; }
    else { PyErr_SetString(PyExc_ValueError,"Unsupported Metal texture format"); return nullptr; }
    MTLTextureDescriptor *d=[MTLTextureDescriptor texture2DDescriptorWithPixelFormat:f width:w height:h mipmapped:NO];
    d.storageMode=MTLStorageModeShared; d.usage=MTLTextureUsageRenderTarget|MTLTextureUsageShaderRead;
    id<MTLTexture> t=[engine.device newTextureWithDescriptor:d];
    if(!t) return PyErr_NoMemory();
    if(pixels!=Py_None) {
        NSData *data=bytes(pixels); if(!data) return nullptr;
        if(data.length!=(NSUInteger)w*h*stride) { PyErr_SetString(PyExc_ValueError,"Texture upload byte size mismatch"); return nullptr; }
        [t replaceRegion:MTLRegionMake2D(0,0,w,h) mipmapLevel:0 withBytes:data.bytes bytesPerRow:w*stride];
    }
    uint64_t identifier=++engine.nextTexture; engine.textures[@(identifier)]=t;
    return PyLong_FromUnsignedLongLong(identifier);
}
static PyObject *deleteTexture(PyObject *,PyObject *arg) {
    uint64_t handle=PyLong_AsUnsignedLongLong(arg); if(PyErr_Occurred()||!ready()) return nullptr;
    // Submitted operations hold strong references independently of this registry.
    [engine.textures removeObjectForKey:@(handle)]; Py_RETURN_NONE;
}
static PyObject *uploadTexture(PyObject *,PyObject *args) {
    unsigned long long handle; int w,h; PyObject *pixels;
    if(!PyArg_ParseTuple(args,"KiiO",&handle,&w,&h,&pixels)||!ready()) return nullptr;
    id<MTLTexture> t=texture(handle); if(!t) return nullptr;
    NSData *data=bytes(pixels); if(!data) return nullptr;
    NSUInteger stride=t.pixelFormat==MTLPixelFormatRGBA32Float ? 16 : t.pixelFormat==MTLPixelFormatRGBA16Float ? 8 : t.pixelFormat==MTLPixelFormatR16Unorm ? 2 : 4;
    if(w<1 || h<1 || (NSUInteger)w>t.width || (NSUInteger)h>t.height || data.length!=(NSUInteger)w*h*stride) { PyErr_SetString(PyExc_ValueError,"Texture upload dimensions/byte size mismatch"); return nullptr; }
    // Ordered with preceding and subsequent draws, not an immediate CPU write
    // into a texture still being sampled by another queued pass.
    NSUInteger pitch=((w*stride+255)/256)*256;
    NSMutableData *padded=[NSMutableData dataWithLength:pitch*h];
    for(int row=0;row<h;row++) memcpy((uint8_t *)padded.mutableBytes+row*pitch,(const uint8_t *)data.bytes+row*w*stride,w*stride);
    id<MTLBuffer> staging=[engine.device newBufferWithBytes:padded.bytes length:padded.length options:MTLResourceStorageModeShared];
    if(!staging) return PyErr_NoMemory();
    [engine.pending addObject:^(id<MTLCommandBuffer> commands) {
        id<MTLBlitCommandEncoder> blit=[commands blitCommandEncoder];
        [blit copyFromBuffer:staging sourceOffset:0 sourceBytesPerRow:pitch sourceBytesPerImage:pitch*h
                 sourceSize:MTLSizeMake(w,h,1) toTexture:t destinationSlice:0 destinationLevel:0 destinationOrigin:MTLOriginMake(0,0,0)];
        [blit endEncoding];
    }];
    Py_RETURN_NONE;
}
static PyObject *clearTexture(PyObject *,PyObject *args) {
    unsigned long long handle; PyObject *color; float c[4];
    if(!PyArg_ParseTuple(args,"KO",&handle,&color)||!ready()||!numbers(color,c,4)) return nullptr;
    id<MTLTexture> target=texture(handle); if(!target) return nullptr;
    MTLClearColor clear=MTLClearColorMake(c[0],c[1],c[2],c[3]);
    [engine.pending addObject:^(id<MTLCommandBuffer> commands) {
        MTLRenderPassDescriptor *p=passFor(target); p.colorAttachments[0].loadAction=MTLLoadActionClear; p.colorAttachments[0].clearColor=clear;
        [[commands renderCommandEncoderWithDescriptor:p] endEncoding];
    }]; Py_RETURN_NONE;
}
static PyObject *drawQuad(PyObject *,PyObject *args) {
    unsigned long long handle; const char *shader,*blend; PyObject *uniforms,*rect,*uv,*sources,*clip;
    if(!PyArg_ParseTuple(args,"KsOOOOsO",&handle,&shader,&uniforms,&rect,&uv,&sources,&blend,&clip)||!ready()) return nullptr;
    id<MTLTexture> target=texture(handle); if(!target) return nullptr;
    float q[12]={0}; if(!numbers(rect,q,4)||!numbers(uv,q+8,4)) return nullptr;
    q[4]=target.width; q[5]=target.height;
    NSData *quad=[NSData dataWithBytes:q length:sizeof(q)], *params=bytes(uniforms); if(!params) return nullptr;
    NSMutableArray<id<MTLTexture>> *textures=[NSMutableArray new]; PyObject *seq=PySequence_Fast(sources,"Texture handles required"); if(!seq) return nullptr;
    for(Py_ssize_t i=0;i<PySequence_Fast_GET_SIZE(seq);i++) { uint64_t handle=PyLong_AsUnsignedLongLong(PySequence_Fast_GET_ITEM(seq,i)); if(PyErr_Occurred()) { Py_DECREF(seq); return nullptr; } id<MTLTexture> t=texture(handle); if(!t) { Py_DECREF(seq); return nullptr; } [textures addObject:t]; }
    Py_DECREF(seq);
    float c[4]={0,0,(float)target.width,(float)target.height}; if(clip!=Py_None && !numbers(clip,c,4)) return nullptr;
    // The fullscreen triangle extends past the quad; viewport-equivalent
    // scissoring confines it to this rectangle before applying caller clip.
    int x0=MAX(0,(int)floorf(MAX(c[0],q[0]))),y0=MAX(0,(int)floorf(MAX(c[1],q[1])));
    int x1=MIN((int)target.width,(int)ceilf(MIN(c[0]+c[2],q[0]+q[2]))),y1=MIN((int)target.height,(int)ceilf(MIN(c[1]+c[3],q[1]+q[3])));
    if(x1<=x0||y1<=y0) Py_RETURN_NONE;
    MTLScissorRect scissor={(NSUInteger)x0,target.height-y1,(NSUInteger)(x1-x0),(NSUInteger)(y1-y0)};
    id<MTLRenderPipelineState> pipeline;
    @try { pipeline=[engine pipeline:[NSString stringWithUTF8String:shader] format:target.pixelFormat blend:[NSString stringWithUTF8String:blend]]; }
    @catch(NSException *e) { PyErr_SetString(PyExc_RuntimeError,e.reason.UTF8String); return nullptr; }
    [engine.pending addObject:^(id<MTLCommandBuffer> commands) {
        id<MTLRenderCommandEncoder> encoder=[commands renderCommandEncoderWithDescriptor:passFor(target)];
        [encoder setRenderPipelineState:pipeline]; [encoder setScissorRect:scissor];
        [encoder setVertexBytes:quad.bytes length:quad.length atIndex:0];
        [encoder setFragmentBytes:quad.bytes length:quad.length atIndex:0];
        if(params.length) [encoder setFragmentBytes:params.bytes length:params.length atIndex:1];
        for(NSUInteger i=0;i<textures.count;i++) [encoder setFragmentTexture:textures[i] atIndex:i];
        [encoder drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3]; [encoder endEncoding];
    }]; Py_RETURN_NONE;
}
static PyObject *setScene(PyObject *,PyObject *arg) {
    unsigned long long handle=PyLong_AsUnsignedLongLong(arg); if(PyErr_Occurred()||!ready()||!texture(handle)) return nullptr;
    engine.scene=handle; Py_RETURN_NONE;
}
static PyObject *drawMesh(PyObject *,PyObject *args) {
    unsigned long long handle,font,context,mask; PyObject *vertices,*indices,*commands,*uniforms;
    if(!PyArg_ParseTuple(args,"KOOOOKKK",&handle,&vertices,&indices,&commands,&uniforms,&font,&context,&mask)||!ready()) return nullptr;
    id<MTLTexture> target=texture(handle), style=texture(context), occlusion=texture(mask);
    if(!target||!style||!occlusion) return nullptr;
    NSData *v=bytes(vertices),*i=bytes(indices),*p=bytes(uniforms); if(!v||!i||!p) return nullptr;
    if(v.length%20 || i.length%4 || p.length!=12*sizeof(float)) { PyErr_SetString(PyExc_ValueError,"ImGui Metal ABI requires 20-byte vertices, 32-bit indices, and 12 float uniforms"); return nullptr; }
    if(!v.length || !i.length) Py_RETURN_NONE;
    id<MTLBuffer> vb=[engine.device newBufferWithBytes:v.bytes length:v.length options:MTLResourceStorageModeShared];
    id<MTLBuffer> ib=[engine.device newBufferWithBytes:i.bytes length:i.length options:MTLResourceStorageModeShared];
    if(!vb||!ib) return PyErr_NoMemory();
    NSMutableArray *rows=[NSMutableArray new]; PyObject *seq=PySequence_Fast(commands,"Mesh commands required"); if(!seq) return nullptr;
    for(Py_ssize_t n=0;n<PySequence_Fast_GET_SIZE(seq);n++) {
        PyObject *row=PySequence_Fast(PySequence_Fast_GET_ITEM(seq,n),"Invalid mesh command");
        if(!row) { Py_DECREF(seq); return nullptr; }
        if(PySequence_Fast_GET_SIZE(row)!=8) { Py_DECREF(row); Py_DECREF(seq); PyErr_SetString(PyExc_ValueError,"Expected (texture,first,count,left,top,right,bottom,channel)"); return nullptr; }
        uint64_t tid=PyLong_AsUnsignedLongLong(PySequence_Fast_GET_ITEM(row,0));
        Py_ssize_t first=PyLong_AsSsize_t(PySequence_Fast_GET_ITEM(row,1)),count=PyLong_AsSsize_t(PySequence_Fast_GET_ITEM(row,2));
        float r[5]; for(int j=0;j<5;j++) r[j]=PyFloat_AsDouble(PySequence_Fast_GET_ITEM(row,j+3));
        Py_DECREF(row);
        if(PyErr_Occurred()) { Py_DECREF(seq); return nullptr; }
        NSUInteger available=i.length/4;
        if(first<0||count<0||(NSUInteger)first>available||(NSUInteger)count>available-(NSUInteger)first) { Py_DECREF(seq); PyErr_SetString(PyExc_ValueError,"Mesh command exceeds index buffer"); return nullptr; }
        const uint32_t *indexData=(const uint32_t *)i.bytes;
        for(NSUInteger offset=(NSUInteger)first;offset<(NSUInteger)first+(NSUInteger)count;offset++) {
            if(indexData[offset]>=v.length/20) { Py_DECREF(seq); PyErr_SetString(PyExc_ValueError,"Mesh index exceeds vertex buffer"); return nullptr; }
        }
        id<MTLTexture> tex=texture(tid); if(!tex) { Py_DECREF(seq); return nullptr; }
        int x0=MAX(0,(int)floorf(r[0])),y0=MAX(0,(int)floorf(r[1]));
        int x1=MIN((int)target.width,(int)ceilf(r[2])),y1=MIN((int)target.height,(int)ceilf(r[3]));
        if(x1<=x0||y1<=y0||!count) continue;
        NSMutableData *params=[p mutableCopy]; float *f=(float *)params.mutableBytes;
        f[7]=tid==font ? 1 : 0; f[11]=r[4];
        [rows addObject:@{ @"texture":tex,@"first":@(first),@"count":@(count),@"x":@(x0),@"y":@(y0),@"w":@(x1-x0),@"h":@(y1-y0),@"params":params }];
    }
    Py_DECREF(seq);
    id<MTLRenderPipelineState> pipeline;
    @try { pipeline=[engine pipeline:@"mesh_fragment" format:target.pixelFormat blend:@"over"]; }
    @catch(NSException *e) { PyErr_SetString(PyExc_RuntimeError,e.reason.UTF8String); return nullptr; }
    [engine.pending addObject:^(id<MTLCommandBuffer> buffer) {
        id<MTLRenderCommandEncoder> encoder=[buffer renderCommandEncoderWithDescriptor:passFor(target)];
        [encoder setRenderPipelineState:pipeline]; [encoder setVertexBuffer:vb offset:0 atIndex:0];
        [encoder setFragmentTexture:style atIndex:1]; [encoder setFragmentTexture:occlusion atIndex:2];
        for(NSDictionary *row in rows) {
            MTLScissorRect rect={[row[@"x"] unsignedIntegerValue],[row[@"y"] unsignedIntegerValue],[row[@"w"] unsignedIntegerValue],[row[@"h"] unsignedIntegerValue]};
            NSData *params=row[@"params"];
            [encoder setScissorRect:rect]; [encoder setVertexBytes:params.bytes length:params.length atIndex:1];
            [encoder setFragmentBytes:params.bytes length:params.length atIndex:1]; [encoder setFragmentTexture:row[@"texture"] atIndex:0];
            [encoder drawIndexedPrimitives:MTLPrimitiveTypeTriangle indexCount:[row[@"count"] unsignedIntegerValue]
                                 indexType:MTLIndexTypeUInt32 indexBuffer:ib indexBufferOffset:[row[@"first"] unsignedIntegerValue]*4];
        }
        [encoder endEncoding];
    }]; Py_RETURN_NONE;
}
static PyObject *drawShape(PyObject *,PyObject *args) {
    unsigned long long handle,mask; PyObject *vertices,*clip; double winz; const char *blend;
    if(!PyArg_ParseTuple(args,"KOKdsO",&handle,&vertices,&mask,&winz,&blend,&clip)||!ready()) return nullptr;
    id<MTLTexture> target=texture(handle),win=texture(mask); if(!target||!win) return nullptr;
    NSData *data=bytes(vertices); if(!data) return nullptr;
    if(data.length%12 || data.length<36) { PyErr_SetString(PyExc_ValueError,"Shadow strip requires at least three float3 vertices"); return nullptr; }
    id<MTLBuffer> buffer=[engine.device newBufferWithBytes:data.bytes length:data.length options:MTLResourceStorageModeShared];
    if(!buffer) return PyErr_NoMemory();
    float c[4]={0,0,(float)target.width,(float)target.height}; if(clip!=Py_None&&!numbers(clip,c,4)) return nullptr;
    int x0=MAX(0,(int)floorf(c[0])), y0=MAX(0,(int)floorf(c[1]));
    int x1=MIN((int)target.width,(int)ceilf(c[0]+c[2])),y1=MIN((int)target.height,(int)ceilf(c[1]+c[3]));
    if(x1<=x0||y1<=y0) Py_RETURN_NONE;
    MTLScissorRect scissor={(NSUInteger)x0,target.height-y1,(NSUInteger)(x1-x0),(NSUInteger)(y1-y0)};
    float uniforms[4]={(float)target.width,(float)target.height,(float)winz,0};
    NSData *params=[NSData dataWithBytes:uniforms length:sizeof(uniforms)];
    id<MTLRenderPipelineState> pipeline;
    @try { pipeline=[engine pipeline:@"shadow_shape_fragment" format:target.pixelFormat blend:[NSString stringWithUTF8String:blend]]; }
    @catch(NSException *e) { PyErr_SetString(PyExc_RuntimeError,e.reason.UTF8String); return nullptr; }
    [engine.pending addObject:^(id<MTLCommandBuffer> commands) {
        id<MTLRenderCommandEncoder> encoder=[commands renderCommandEncoderWithDescriptor:passFor(target)];
        [encoder setRenderPipelineState:pipeline]; [encoder setScissorRect:scissor];
        [encoder setVertexBuffer:buffer offset:0 atIndex:0];
        [encoder setVertexBytes:params.bytes length:params.length atIndex:1];
        [encoder setFragmentBytes:params.bytes length:params.length atIndex:1];
        [encoder setFragmentTexture:win atIndex:0];
        [encoder drawPrimitives:MTLPrimitiveTypeTriangleStrip vertexStart:0 vertexCount:data.length/12];
        [encoder endEncoding];
    }]; Py_RETURN_NONE;
}
static PyObject *programManifest(PyObject *,PyObject *) {
    NSURL *url=[NSBundle.mainBundle URLForResource:@"metal-programs" withExtension:@"json"];
    NSError *error=nil;
    NSString *json=url ? [NSString stringWithContentsOfURL:url encoding:NSUTF8StringEncoding error:&error] : nil;
#if MELTY_METAL_TESTING
    if(testManifest) json=testManifest;
#endif
    if(!json) { PyErr_SetString(PyExc_RuntimeError, error.localizedDescription.UTF8String ?: "metal-programs.json is missing"); return nullptr; }
    return PyUnicode_FromString(json.UTF8String);
}
#if MELTY_METAL_TESTING
// Test-only entry points execute the identical command encoder on host Metal.
// They are absent from the device executable and never create a window.
static PyObject *testInitialize(PyObject *,PyObject *args) {
    const char *library,*manifest;
    if(!PyArg_ParseTuple(args,"ss",&library,&manifest)) return nullptr;
    MeltyMetalRenderer *renderer=[MeltyMetalRenderer new];
    renderer.device=MTLCreateSystemDefaultDevice(); renderer.owner=NSThread.currentThread;
    if(!renderer.device) { PyErr_SetString(PyExc_RuntimeError,"No Metal device available"); return nullptr; }
    NSError *error=nil;
    renderer.library=[renderer.device newLibraryWithURL:[NSURL fileURLWithPath:[NSString stringWithUTF8String:library]] error:&error];
    if(!renderer.library) { PyErr_SetString(PyExc_RuntimeError,error.localizedDescription.UTF8String); return nullptr; }
    testManifest=[NSString stringWithContentsOfFile:[NSString stringWithUTF8String:manifest] encoding:NSUTF8StringEncoding error:&error];
    if(!testManifest) { PyErr_SetString(PyExc_RuntimeError,error.localizedDescription.UTF8String); return nullptr; }
    renderer.textures=[NSMutableDictionary new];renderer.pipelines=[NSMutableDictionary new];renderer.pending=[NSMutableArray new];
    engine=renderer; Py_RETURN_NONE;
}
static PyObject *testFlush(PyObject *,PyObject *) {
    if(!ready()) return nullptr;
    @try {
        id<MTLCommandQueue> queue=[engine.device newCommandQueue];
        id<MTLCommandBuffer> commands=[queue commandBuffer];
        NSArray<MetalOperation> *operations=[engine.pending copy];[engine.pending removeAllObjects];
        for(MetalOperation operation in operations) operation(commands);
        [commands commit];[commands waitUntilCompleted];
        if(commands.error) { PyErr_SetString(PyExc_RuntimeError,commands.error.localizedDescription.UTF8String); return nullptr; }
    } @catch(NSException *e) { PyErr_SetString(PyExc_RuntimeError,e.reason.UTF8String);return nullptr; }
    Py_RETURN_NONE;
}
static PyObject *testPixels(PyObject *,PyObject *arg) {
    unsigned long long handle=PyLong_AsUnsignedLongLong(arg); if(PyErr_Occurred()||!ready()) return nullptr;
    id<MTLTexture> t=texture(handle);if(!t) return nullptr;
    NSUInteger stride=t.pixelFormat==MTLPixelFormatRGBA32Float ? 16 : t.pixelFormat==MTLPixelFormatRGBA16Float ? 8 : t.pixelFormat==MTLPixelFormatR16Unorm ? 2 : 4;
    PyObject *result=PyBytes_FromStringAndSize(nullptr,t.width*t.height*stride);if(!result)return nullptr;
    [t getBytes:PyBytes_AS_STRING(result) bytesPerRow:t.width*stride fromRegion:MTLRegionMake2D(0,0,t.width,t.height) mipmapLevel:0];
    return result;
}
static PyObject *testPresent(PyObject *,PyObject *arg) {
    unsigned long long handle=PyLong_AsUnsignedLongLong(arg); if(PyErr_Occurred()||!ready()) return nullptr;
    id<MTLTexture> target=texture(handle); if(!target) return nullptr;
    id<MTLCommandQueue> queue=[engine.device newCommandQueue];
    id<MTLCommandBuffer> commands=[queue commandBuffer];
    NSError *error=nil;
    if(![engine encodeFrame:commands target:target error:&error]) {
        PyErr_SetString(PyExc_RuntimeError,error.localizedDescription.UTF8String); return nullptr;
    }
    [commands commit]; [commands waitUntilCompleted];
    if(commands.error) { PyErr_SetString(PyExc_RuntimeError,commands.error.localizedDescription.UTF8String); return nullptr; }
    Py_RETURN_NONE;
}
#endif
static PyMethodDef methods[]={
#if MELTY_METAL_TESTING
    {"_test_initialize",testInitialize,METH_VARARGS,nullptr},
    {"_test_flush",testFlush,METH_NOARGS,nullptr},{"_test_pixels",testPixels,METH_O,nullptr},
    {"_test_present",testPresent,METH_O,nullptr},
#endif
    {"program_manifest",programManifest,METH_NOARGS,nullptr},
    {"create_texture",createTexture,METH_VARARGS,nullptr},{"delete_texture",deleteTexture,METH_O,nullptr},
    {"upload_texture",uploadTexture,METH_VARARGS,nullptr},{"clear_texture",clearTexture,METH_VARARGS,nullptr},
    {"quad",drawQuad,METH_VARARGS,nullptr},{"set_scene",setScene,METH_O,nullptr},
    {"mesh",drawMesh,METH_VARARGS,nullptr},{"shape",drawShape,METH_VARARGS,nullptr},
    {nullptr,nullptr,0,nullptr},
};
static PyModuleDef module={PyModuleDef_HEAD_INIT,"_melty_metal",nullptr,-1,methods,nullptr,nullptr,nullptr,nullptr};
PyMODINIT_FUNC PyInit__melty_metal(void) { return PyModule_Create(&module); }
