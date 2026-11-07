// Isolated lifecycle attribution against the already loaded ImGui implementation.
#include "imgui.h"
#include <chrono>
#include <cstdint>

using Clock = std::chrono::steady_clock;
static double ns(Clock::time_point a, Clock::time_point b) {
    return std::chrono::duration<double, std::nano>(b-a).count();
}
extern "C" const char* probe_header_version() { return IMGUI_VERSION; }
extern "C" uintptr_t probe_context() { return reinterpret_cast<uintptr_t>(ImGui::GetCurrentContext()); }
extern "C" double probe_new_frame() { auto a=Clock::now(); ImGui::NewFrame();return ns(a,Clock::now()); }
extern "C" double probe_render() { auto a=Clock::now(); ImGui::Render();return ns(a,Clock::now()); }
extern "C" double probe_end() { auto a=Clock::now(); ImGui::End();return ns(a,Clock::now()); }
extern "C" double probe_cycles(const uintptr_t* contexts, int count, int content, int commands, double* phases) {
    for (int i=0;i<7;i++) phases[i]=0;
    auto all=Clock::now();
    for (int i=0;i<count;i++) {
        auto a=Clock::now();
        ImGui::SetCurrentContext(reinterpret_cast<ImGuiContext*>(contexts[i]));
        auto b=Clock::now(); phases[0]+=ns(a,b);
        ImGui::NewFrame();
        auto c=Clock::now(); phases[1]+=ns(b,c);
        ImGui::SetNextWindowPos(ImVec2(0,0));
        ImGui::SetNextWindowSize(ImVec2(128,64));
        ImGui::Begin("lifecycle",nullptr,ImGuiWindowFlags_NoTitleBar|ImGuiWindowFlags_NoResize|
            ImGuiWindowFlags_NoMove|ImGuiWindowFlags_NoScrollbar|ImGuiWindowFlags_NoSavedSettings|
            ImGuiWindowFlags_NoScrollWithMouse|ImGuiWindowFlags_NoBackground);
        ImGui::SetCursorScreenPos(ImVec2(0,0));
        ImGui::GetWindowDrawList()->PushClipRect(ImVec2(0,0),ImVec2(128,64),false);
        auto d=Clock::now();phases[2]+=ns(c,d);
        ImGui::PushID("gui-lifecycle");ImGui::BeginGroup();
        auto e=Clock::now();phases[3]+=ns(d,e);
        if (content==1) for(int j=0;j<commands;j++)
            ImGui::GetWindowDrawList()->AddRectFilled(ImVec2(0,0),ImVec2(8,2),0xff0000ff);
        if (content==2) ImGui::Button("##cell",ImVec2(8,2));
        auto f=Clock::now();phases[4]+=ns(e,f);
        ImGui::EndGroup();ImGui::PopID();ImGui::GetItemRectSize();
        ImGui::GetWindowDrawList()->PopClipRect();ImGui::End();
        auto g=Clock::now();phases[5]+=ns(f,g);
        ImGui::Render();
        auto h=Clock::now();phases[6]+=ns(g,h);
    }
    return ns(all,Clock::now());
}
