# Tensor rendering: hard requirements

Set by Lukas, 2026-09-18. These are requirements, not preferences. A design, a
fallback or a "simplification" that breaks one of them is wrong however small it
looks. Historical code, comments and reviews do not override this page.

## 0. Two raymarchers, both supported

Melty has **two** volume raymarchers and both are first-class. Neither is a
fallback for the other, neither is legacy, neither gets removed or left to rot:

- the **OpenGL raymarcher**, on the display GPU (the RTX 4090), over a 3-D texture;
- the **CUDA raymarcher**, in place on whichever GPU holds the tensor.

A change to volume rendering (a shading feature, a transfer function, a label,
a camera control) lands in both, and both are tested.

## 1. The tensor never moves

A CUDA tensor is raymarched **in place, on the GPU that holds it**. Melty renders
tensors upwards of **80 GB at 120 fps**; such a tensor cannot be transferred, not
once and not per frame.

Never, on any path that draws a CUDA tensor:

- copy it to the CPU (`.cpu()`, `.numpy()`, `torch.save`, a staging file);
- copy it to another GPU, including a peer copy to the display GPU (so a
  tensor on the H100 is never turned into a texture on the 4090);
- make a dense duplicate of it on its own GPU (`contiguous()`, a dtype
  conversion of the whole tensor, a repack) as a routine step of drawing.

The working machine shows the case: the tensor lives on the **H100**, the window
is on an **RTX 4090**. The march runs on the H100.

What may be allocated on the tensor's GPU is what the march itself needs and
whose size does not follow the tensor: the output image, the LUT, the shading
parameters, the low-resolution traversal mip, the floor map. Genuine transforms
the user asks for (sort, mean, densify) produce a result on the tensor's own
GPU, once per setting change, never per frame.

When in-place rendering is impossible (no CUDA marcher, memory the driver cannot
share, a torch outside the tested range), **show an error that says so**. Do not
fall back to a copy. The existing message "the tensor was not copied to the CPU"
is the model.

## 2. Only the image crosses

The rendered image is what travels from the tensor's GPU to the display: about
3 MB (width × height RGBA16F). **How it travels does not matter**: a pinned host
buffer, a CUDA buffer, a peer copy, a file descriptor. Choose whatever is
simplest and fast enough for 120 fps. No design may be rejected, and no tensor
may be moved, for the sake of the image path.

A shared OpenGL context between processes is not wanted and not needed.

Routes in the code (2026-09-18), chosen in `texture_model._upload_cuda_image`:

- **GPU to GPU** (`cuda_texture_model.image_to_texture`, `Toggles.Voxels.cuda_image_interop`):
  the image pointer, this process's or one opened from another process by CUDA
  handle, is copied into a CUDA-registered pixel buffer on the display GPU (a
  peer copy from another GPU; without peer access the driver stages it itself)
  and becomes a texture inside that GPU's VRAM. Needs CUDA behind the GL context.
- **Pinned host buffer + `glTexSubImage2D`**: everywhere else (a display GPU CUDA
  cannot reach). Must keep working; it is the route a shipped product falls back to.

## 3. Consequences for the design

- **Both raymarchers are kept at parity** (section 0). The OpenGL raymarcher
  renders on the 4090: CPU arrays and tensors, `GLTexture` inputs, and CUDA
  tensors that live on the 4090, which reach their texture through CUDA-GL
  interop on that same GPU, never through the host. The CUDA raymarcher renders
  any CUDA tensor where it lies.
- **Automatic backend selection never moves a tensor off its GPU.** A tensor on
  the H100 is marched by CUDA on the H100.
- **Project code runs in its own process** with its own interpreter and its own
  torch; several projects with different torch versions are open at once. The
  project process shares its allocation through a CUDA driver IPC handle
  (`meltygui_pro/models/cuda_shared_memory.py`); Melty maps the pointer and
  marches it. Neither side's torch version takes part in the hand-off.
- **Where the march runs is free**, as long as it runs on the tensor's GPU:
  in Melty's process over the mapped pointer (today), or in the process that
  owns the tensor, which then ships the image. Requirement 2 is what makes the
  second one possible.
- **Melty does not need torch to draw a tensor.** Views match by class and
  package name; the kernels are `meltygui_pycuda`. What a kernel needs is the
  pointer, shape, strides, dtype and device of the buffer. Work that needs torch
  belongs to the process that owns the tensor.
- Multi-GPU: match devices by **UUID**, never by index (`CUDA_VISIBLE_DEVICES`
  differs between processes).

## 4. Reviewing a change against this page

Ask of every change on the tensor path:

1. Does any byte of the tensor leave its GPU, or get duplicated on it per frame?
2. Does allocation on the tensor's GPU grow with the tensor?
3. On failure, does the user see an error, or does something get copied quietly?
4. Does it hold at 80 GB and 120 fps, not only at the 100×100×100 test volume?

Known places that copy today and must stay off the CUDA-tensor path:
`voxel_view.py` `gl_state.texture3d("volume", vol.cpu().numpy(), …)` (the OpenGL
upload fallback), `cuda_texture_model.tensor_to_texture` (copy into a GL texture: right on
the display GPU, never across GPUs), `slice_volume` (materializes; `slice_volume_view` is the in-place
twin), and the Windows capture path in
`meltygui_pro/models/project_function_worker.py`, which saves CUDA tensors to a
file because torch has no CUDA IPC there.
