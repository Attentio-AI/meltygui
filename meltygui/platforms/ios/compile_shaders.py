#!/usr/bin/env python3
"""Build finite Melty shader ports into MSL. This is not a runtime GLSL API.

The existing built-in fragment math stays authoritative. Only the declared
programs below are supported; Metal's compiler validates every generated port.
"""
from __future__ import annotations

from meltygui.platforms.ios import build_directory
BUILD = build_directory()
import argparse
import ast
import json
import hashlib
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
PROGRAMS = {
    "mask": "_MASK_FS", "mask_rounded": "_MASK_ROUNDED_FS",
    "mask_textured": "_MASK_TEXTURED_FS", "mask_textured_rounded": "_MASK_TEXTURED_ROUNDED_FS",
    "mask_offset": "_MASK_TEXTURED_OFFSET_FS", "mask_offset_rounded": "_MASK_TEXTURED_OFFSET_ROUNDED_FS",
    "tile_copy": "_COPY_FS", "shadow_gradient": "_SHADOW_GRAD_FS", "glow": "_GLOW_FS",
}
TYPES = {"vec2":"float2", "vec3":"float3", "vec4":"float4", "ivec2":"int2", "ivec3":"int3", "ivec4":"int4", "mat3":"float3x3", "mat4":"float4x4"}


def constants(path):
    result = {}
    for node in ast.parse(Path(path).read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name):
            if isinstance(node.value,ast.Constant) and isinstance(node.value.value,str):
                result[node.targets[0].id]=node.value.value
    return result


def port(name, source):
    source=re.sub(r"//[^\n]*|/\*.*?\*/", "", source, flags=re.S)
    source=re.sub(r"^\s*#version[^\n]*", "", source, flags=re.M)
    uniforms=re.findall(r"\buniform\s+(\w+)\s+(\w+)\s*;",source)
    source=re.sub(r"\buniform\s+\w+\s+\w+\s*;", "",source)
    varying=re.findall(r"\b(?:in|out)\s+(\w+)\s+(\w+)\s*;",source)
    source=re.sub(r"\b(?:in|out)\s+\w+\s+\w+\s*;", "",source)
    if any(type_ not in ("vec2","vec4") for type_,_ in varying):
        raise ValueError(f"{name}: unsupported varying contract {varying}")
    source=re.sub(r"\btexture\s*\(","sample_gl(",source)
    source=re.sub(r"\btexelFetch\s*\(","read_gl(",source)
    source=re.sub(r"\btextureSize\s*\(","size_gl(",source)
    source=source.replace("discard;","discard_fragment();")
    if name in ("shadow_composite","brightness_contrast"):
        for sampler in ("u_texture", "shadow_map", "glow_map"):
            source=re.sub(rf"\bsample_gl\(\s*{sampler}\s*,",f"sample_linear_gl({sampler},",source)
    for before,after in TYPES.items(): source=re.sub(rf"\b{before}\b",after,source)
    fields,values,arguments,metadata=[],[],[],[]
    slot=texture=0
    for type_,field in uniforms:
        if type_=="sampler2D":
            fields.append(f"texture2d<float> {field};")
            values.append(f"t{texture}")
            arguments.append(f"texture2d<float> t{texture} [[texture({texture})]]")
            metadata.append({"name":field,"type":type_,"texture":texture}); texture+=1
        else:
            fields.append(f"{TYPES.get(type_,type_)} {field};")
            swizzle={"float":"x","int":"x","bool":"x","vec2":"xy","vec3":"xyz","vec4":"xyzw"}[type_]
            value=f"u[{slot}].{swizzle}"
            values.append(f"{TYPES.get(type_,type_)}({value})")
            metadata.append({"name":field,"type":type_,"slot":slot}); slot+=1
    for type_,field in varying:
        fields.append(f"{TYPES[type_]} {field};")
        values.append("in.uv" if type_=="vec2" else "float4(0)")
    fields.append("float4 gl_FragCoord;")
    values.append("float4(in.position.x,quad.size.y-in.position.y,0,1)")
    args=", "+", ".join(arguments) if arguments else ""
    output=next((field for type_,field in varying if type_=="vec4"),None)
    if output is None: raise ValueError(f"{name}: missing fragment output")
    msl=f"""
struct Program_{name} {{
    {' '.join(fields)}
    {source}
}};
fragment float4 {name}_fragment(QuadOut in [[stage_in]], constant QuadUniforms &quad [[buffer(0)]],
    constant float4 *u [[buffer(1)]]{args}) {{
    Program_{name} p{{{', '.join(values)}}}; p.main(); return p.{output};
}}
"""
    return msl,metadata


def text_policy(toolkit):
    package = Path(toolkit)
    if (package / "meltygui").is_dir():
        package = package / "meltygui"
    tree=ast.parse((package / "core/styling/style.py").read_text())
    function=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=="adjust_text_color")
    returned=next(node for node in function.body if isinstance(node,ast.Return))
    original=ast.literal_eval(returned.value)
    source=re.sub(r"//[^\n]*|/\*.*?\*/", "", original, flags=re.S)
    declarations=re.findall(r"\buniform\s+(\w+)\s+(\w+)\s*;",source)
    if declarations != [("sampler2D","StyleContext"),("int","DynamicStyles"),("float","TextContrast")]:
        raise ValueError("Text policy uniform contract changed; update its Metal port")
    source=re.sub(r"\buniform\s+\w+\s+\w+\s*;", "", source)
    for before,after in TYPES.items(): source=re.sub(rf"\b{before}\b",after,source)
    source=re.sub(r"\btexelFetch\s*\(","read_gl(",source)
    msl="""
struct MeltyTextPolicy {
    texture2d<float> StyleContext;
    int DynamicStyles;
    float TextContrast;
    float4 gl_FragCoord;
"""+source+"""
};
float3 melty_text_policy(float3 suggestion,texture2d<float> context,float2 point,float contrast) {
    MeltyTextPolicy p{context,1,contrast,float4(point.x,float(context.get_height())-point.y,0,1)};
    return p.adjust_text_color(suggestion);
}
"""
    return msl,hashlib.sha256(original.encode()).hexdigest()


def generate(toolkit, output):
    package = Path(toolkit)
    if (package / "meltygui").is_dir():
        package = package / "meltygui"
    values=constants(package / "core/cache/tile_cache.py")
    source=(ROOT/"Renderer/MetalShaders.metal").read_text()
    policy,policy_hash=text_policy(toolkit)
    source=source.replace("/* MELTY_TEXT_POLICY */",policy)
    manifest={"_text_policy_sha256":policy_hash}
    for name,variable in PROGRAMS.items():
        if variable not in values: raise ValueError(f"Missing source shader {variable}; update its declared Metal port")
        msl,metadata=port(name,values[variable]); source+=msl; manifest[name]=metadata
    classes = ast.parse((package / "graphics/shaders.py").read_text())
    for node in classes.body:
        if not isinstance(node,ast.ClassDef) or node.name not in ("ShadowCast","ShadowComposite","BrightnessContrast"):
            continue
        fields={n.targets[0].id:n.value for n in node.body if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name)}
        env={"__builtins__":{},"GLType":SimpleNamespace(**{name:value for name,value in (
            ("FLOAT","float"),("INT","int"),("BOOL","bool"),("VEC2","vec2"),("VEC3","vec3"),("VEC4","vec4"),("SAMPLER2D","sampler2D"))})}
        for field,value in fields.items():
            if field not in ("uniforms","fragment_code"):
                try: env[field]=eval(compile(ast.Expression(value),"<shader constants>","eval"),env)
                except (NameError,TypeError): pass
        uniforms=eval(compile(ast.Expression(fields["uniforms"]),"<shader uniforms>","eval"),env)
        fragment=ast.literal_eval(fields["fragment_code"])
        declarations="in vec2 v_texcoord; out vec4 fragColor; uniform sampler2D u_texture;\n"
        declarations+="\n".join(f"uniform {type_} {key};" for key,(type_,_) in uniforms.items())
        name=re.sub(r"(?<!^)(?=[A-Z])","_",node.name).lower()
        msl,metadata=port(name,declarations+fragment); source+=msl; manifest[name]=metadata
        for entry in metadata:
            if entry["name"] in uniforms:
                entry["default"] = uniforms[entry["name"]][1]
    style = """
    uniform sampler2D palette;
    uniform float palette_index;
    uniform vec2 rect_size;
    uniform float radius;
    in vec2 vUV;
    out vec4 oColor;
    void main() {
        vec2 half_size = rect_size * 0.5;
        float r = min(radius, min(half_size.x, half_size.y));
        vec2 q = abs((vUV-0.5)*rect_size) - (half_size-vec2(r));
        float d = length(max(q,0.0)) + min(max(q.x,q.y),0.0)-r;
        float coverage = clamp(0.5-d,0.0,1.0);
        if (coverage <= 0.0) discard;
        oColor=vec4(texelFetch(palette,ivec2(int(palette_index+0.5),0),0).rgb,coverage);
    }
    """
    msl, metadata = port("style_context", style)
    source += msl
    manifest["style_context"] = metadata
    output=Path(output); output.mkdir(parents=True,exist_ok=True)
    (output/"Melty.metal").write_text(source)
    (output/"metal-programs.json").write_text(json.dumps(manifest,indent=2)+"\n")
    return output/"Melty.metal"


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolkit",type=Path,default=ROOT.parents[1])
    parser.add_argument("--output",type=Path,default=BUILD / "shaders")
    parser.add_argument("--sdk",default="iphoneos",choices=("iphoneos","macosx"))
    args=parser.parse_args()
    source=generate(args.toolkit,args.output)
    air=args.output/"Melty.air"
    subprocess.run(["xcrun","--sdk",args.sdk,"metal","-std=metal3.0","-c",str(source),"-o",str(air)],check=True)
    subprocess.run(["xcrun","--sdk",args.sdk,"metallib",str(air),"-o",str(args.output/"Melty.metallib")],check=True)


if __name__=="__main__": main()
