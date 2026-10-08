cmake_minimum_required(VERSION 3.20)
project(naksha_vulkan LANGUAGES CXX)
enable_language(C)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

# ---- Toolchain hints (no internet; tools live under H:/tools) ----
if(DEFINED ENV{VULKAN_SDK})
    list(APPEND CMAKE_PREFIX_PATH $ENV{VULKAN_SDK})
endif()
if(NOT CMAKE_CXX_COMPILER)
    set(CMAKE_CXX_COMPILER "H:/tools/mingw1310_64/bin/g++.exe")
endif()
if(NOT CMAKE_C_COMPILER)
    set(CMAKE_C_COMPILER "H:/tools/mingw1310_64/bin/gcc.exe")
endif()
if(NOT CMAKE_MAKE_PROGRAM)
    set(CMAKE_MAKE_PROGRAM "H:/tools/Ninja/ninja.exe")
endif()

# MSVC: windows.h defines min()/max() macros that break std::max(, ) in this
# codebase (C2589). Nominal (MinGW) builds are unaffected, so this is applied
# only where it is needed.
if(MSVC)
    add_compile_definitions(NOMINMAX WIN32_LEAN_AND_MEAN)
endif()

# ---- Vulkan + dependencies ----
find_package(Vulkan REQUIRED)
find_path(VOLK_INCLUDE_DIR volk.h
    HINTS "$ENV{VULKAN_SDK}/Include" "$ENV{VOLK_INCLUDE}"
    PATHS "H:/tools/Include/Volk" "H:/tools/Include"
    PATH_SUFFIXES Volk)
find_path(VMA_INCLUDE_DIR vk_mem_alloc.h
    HINTS "$ENV{VULKAN_SDK}/Include" "$ENV{VMA_INCLUDE}"
    PATHS "H:/tools/Include/vma" "H:/tools/Include"
    PATH_SUFFIXES vma)
find_program(GLSLC glslc
    HINTS "$ENV{VULKAN_SDK}/Bin" "$ENV{GLSLC}"
    PATHS "H:/tools/Bin")

add_library(volk STATIC "${VOLK_INCLUDE_DIR}/../Volk/volk.c")
target_include_directories(volk PUBLIC "${VOLK_INCLUDE_DIR}" "${VMA_INCLUDE_DIR}")
target_compile_definitions(volk PUBLIC VK_NO_PROTOTYPES VK_USE_PLATFORM_WIN32_KHR)
target_link_libraries(volk PUBLIC Vulkan::Headers)

# ---- Shader compile rule (explicit per-file dependency) ----
set(SHADER_DIR "${CMAKE_CURRENT_SOURCE_DIR}/shaders")
set(SPIRV_DIR "${CMAKE_CURRENT_BINARY_DIR}/shaders")
file(MAKE_DIRECTORY "${SPIRV_DIR}")

function(naksha_compile_shader NAME)
    add_custom_command(
        OUTPUT "${SPIRV_DIR}/${NAME}.spv"
        COMMAND "${GLSLC}" --target-env=vulkan1.3
                "${SHADER_DIR}/${NAME}"
                -o "${SPIRV_DIR}/${NAME}.spv"
        DEPENDS "${SHADER_DIR}/${NAME}"
        COMMENT "Compiling ${NAME}"
    )
    add_custom_target(naksha_shader_${NAME} ALL
        DEPENDS "${SPIRV_DIR}/${NAME}.spv")
endfunction()

naksha_compile_shader(point.vert)
naksha_compile_shader(point.frag)
naksha_compile_shader(surface.vert)
naksha_compile_shader(surface.frag)
naksha_compile_shader(surface_indexed.vert)
naksha_compile_shader(surface_indexed.frag)
# Instant Shaded lighting pass (fullscreen; reads the stored oct16 normal).
naksha_compile_shader(instant.vert)
naksha_compile_shader(instant.frag)
# Instant Shaded SPLAT pass: class colour + the STORED oct16x2 normal.
naksha_compile_shader(splat.vert)
naksha_compile_shader(splat.frag)
set(SHADER_TARGETS naksha_shader_point.vert naksha_shader_point.frag
                    naksha_shader_surface.vert naksha_shader_surface.frag
                    naksha_shader_surface_indexed.vert naksha_shader_surface_indexed.frag
                    naksha_shader_instant.vert naksha_shader_instant.frag
                    naksha_shader_splat.vert naksha_shader_splat.frag)

# ---- Core library ----
set(CORE_SOURCES
    src/core/vulkan/VmaImplementation.cpp
    src/core/vulkan/VulkanAllocator.cpp
    src/core/vulkan/VulkanCommandPool.cpp
    src/core/vulkan/VulkanContext.cpp
    src/core/vulkan/VulkanDescriptorManager.cpp
    src/core/vulkan/VulkanDevice.cpp
    src/core/vulkan/VulkanFrameManager.cpp
    src/core/vulkan/VulkanInstance.cpp
    src/core/vulkan/VulkanPhysicalDevice.cpp
    src/core/vulkan/VulkanPipelineManager.cpp
    src/core/vulkan/VulkanRenderPass.cpp
    src/core/vulkan/VulkanShaderManager.cpp
    src/core/vulkan/VulkanSwapchain.cpp
    src/core/renderer/CoreCamera.cpp
    src/core/renderer/Camera.cpp
    src/Buffer.cpp
    src/Renderer.cpp
    src/PointCloudRenderer.cpp
    src/SurfaceRenderer.cpp
    src/SurfaceTiling.cpp
)

add_library(naksha_vulkan_core STATIC ${CORE_SOURCES})
target_include_directories(naksha_vulkan_core PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/include"
    "${VOLK_INCLUDE_DIR}"
    "${VMA_INCLUDE_DIR}")
target_link_libraries(naksha_vulkan_core PUBLIC volk)
target_compile_definitions(naksha_vulkan_core PUBLIC
    VK_NO_PROTOTYPES VK_USE_PLATFORM_WIN32_KHR
    VMA_STATIC_VULKAN_FUNCTIONS=0
    VMA_DYNAMIC_VULKAN_FUNCTIONS=1)
add_dependencies(naksha_vulkan_core ${SHADER_TARGETS})

# ---- Benchmark harness ----
add_executable(naksha_vulkan_benchmark
    tests/benchmark_main.cpp
)
target_link_libraries(naksha_vulkan_benchmark PRIVATE
    naksha_vulkan_core
    user32 gdi32)
add_dependencies(naksha_vulkan_benchmark ${SHADER_TARGETS})
# benchmark_main.cpp defines WinMain (it owns a Win32 message loop for the
# interactive probe), but WIN32_EXECUTABLE OFF made the linker emit
# /subsystem:console, which requires a `main` the file does not define:
#   LNK2019: unresolved external symbol main referenced in __scrt_common_main_seh
# The executable subsystem flag is what has to match the entry point.
set_target_properties(naksha_vulkan_benchmark PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}"
    WIN32_EXECUTABLE ON)
add_custom_command(TARGET naksha_vulkan_benchmark POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SPIRV_DIR}/point.vert.spv" "$<TARGET_FILE_DIR:naksha_vulkan_benchmark>/point.vert.spv"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SPIRV_DIR}/point.frag.spv" "$<TARGET_FILE_DIR:naksha_vulkan_benchmark>/point.frag.spv"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SPIRV_DIR}/surface.vert.spv" "$<TARGET_FILE_DIR:naksha_vulkan_benchmark>/surface.vert.spv"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SPIRV_DIR}/surface.frag.spv" "$<TARGET_FILE_DIR:naksha_vulkan_benchmark>/surface.frag.spv"
)
# ---- Shared library C ABI (Python/ctypes bridge, no pybind11 available) ----
add_library(naksha_vulkan SHARED
    src/naksha_vulkan_c_api.cpp
)
target_link_libraries(naksha_vulkan PRIVATE naksha_vulkan_core user32 gdi32)
# Statically link the MinGW runtime so the DLL has no libgcc/libstdc++/
# libwinpthread dependency at load time - Python (via ctypes.CDLL) loads
# this DLL with only the calling process's PATH, which does not include
# H:/tools/mingw1310_64/bin, so a dynamic dependency on those runtime DLLs
# would make every load fail with FileNotFoundError.
target_link_options(naksha_vulkan PRIVATE -static-libgcc -static-libstdc++ -static -lwinpthread)
target_compile_definitions(naksha_vulkan PRIVATE NAKSHA_VULKAN_C_API_BUILD)
set_target_properties(naksha_vulkan PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}"
    PREFIX "")
add_dependencies(naksha_vulkan ${SHADER_TARGETS})
add_custom_command(TARGET naksha_vulkan POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SPIRV_DIR}/point.vert.spv" "$<TARGET_FILE_DIR:naksha_vulkan>/point.vert.spv"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SPIRV_DIR}/point.frag.spv" "$<TARGET_FILE_DIR:naksha_vulkan>/point.frag.spv"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SPIRV_DIR}/surface.vert.spv" "$<TARGET_FILE_DIR:naksha_vulkan>/surface.vert.spv"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SPIRV_DIR}/surface.frag.spv" "$<TARGET_FILE_DIR:naksha_vulkan>/surface.frag.spv"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SPIRV_DIR}/surface_indexed.vert.spv" "$<TARGET_FILE_DIR:naksha_vulkan>/surface_indexed.vert.spv"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SPIRV_DIR}/surface_indexed.frag.spv" "$<TARGET_FILE_DIR:naksha_vulkan>/surface_indexed.frag.spv"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SPIRV_DIR}/instant.vert.spv" "$<TARGET_FILE_DIR:naksha_vulkan>/instant.vert.spv"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SPIRV_DIR}/instant.frag.spv" "$<TARGET_FILE_DIR:naksha_vulkan>/instant.frag.spv"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SPIRV_DIR}/splat.vert.spv" "$<TARGET_FILE_DIR:naksha_vulkan>/splat.vert.spv"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SPIRV_DIR}/splat.frag.spv" "$<TARGET_FILE_DIR:naksha_vulkan>/splat.frag.spv"
)

add_executable(naksha_startup_probe diagnostics/startup_probe.cpp)
target_link_libraries(naksha_startup_probe PRIVATE user32)
add_executable(naksha_startup_core_probe diagnostics/startup_core_probe.cpp)
target_link_libraries(naksha_startup_core_probe PRIVATE naksha_vulkan_core user32)

# ---- Tiling / LOD / frustum unit checks -----------------------------------
# Pure CPU, no Vulkan and no window: this is the regression gate for the
# spatial logic the draw path depends on (no holes, no duplicate triangles,
# contiguous tile runs, conservative AABB culling, real LOD reduction).
add_executable(naksha_vulkan_tiling_test tests/tiling_test_main.cpp src/SurfaceTiling.cpp)
target_include_directories(naksha_vulkan_tiling_test PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/include")
set_target_properties(naksha_vulkan_tiling_test PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}")

# End-to-end acceptance test for the tiled surface path, driven through the real
# nkv_* C ABI on a real window with the validation layer ENABLED. Separate from
# the pure-CPU spatial tests above because it needs a live swapchain.
add_executable(naksha_surface_acceptance
    tests/surface_acceptance_main.cpp
    src/naksha_vulkan_c_api.cpp)
target_include_directories(naksha_surface_acceptance PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/include")
target_link_libraries(naksha_surface_acceptance PRIVATE naksha_vulkan_core user32 gdi32)
set_target_properties(naksha_surface_acceptance PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}")
add_dependencies(naksha_surface_acceptance ${SHADER_TARGETS})
add_custom_command(TARGET naksha_surface_acceptance POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SPIRV_DIR}/surface.vert.spv" "$<TARGET_FILE_DIR:naksha_surface_acceptance>/surface.vert.spv"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SPIRV_DIR}/surface.frag.spv" "$<TARGET_FILE_DIR:naksha_surface_acceptance>/surface.frag.spv"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SPIRV_DIR}/point.vert.spv" "$<TARGET_FILE_DIR:naksha_surface_acceptance>/point.vert.spv"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SPIRV_DIR}/point.frag.spv" "$<TARGET_FILE_DIR:naksha_surface_acceptance>/point.frag.spv"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SPIRV_DIR}/instant.vert.spv" "$<TARGET_FILE_DIR:naksha_surface_acceptance>/instant.vert.spv"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SPIRV_DIR}/instant.frag.spv" "$<TARGET_FILE_DIR:naksha_surface_acceptance>/instant.frag.spv"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SPIRV_DIR}/splat.vert.spv" "$<TARGET_FILE_DIR:naksha_surface_acceptance>/splat.vert.spv"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SPIRV_DIR}/splat.frag.spv" "$<TARGET_FILE_DIR:naksha_surface_acceptance>/splat.frag.spv"
)


