@echo off
set "PATH=H:\tools\mingw1310_64\bin;H:\tools\CMake_64\bin;H:\tools\Ninja;H:\tools\Bin;H:\tools\Lib;H:\tools\Include;%PATH%"
set "VULKAN_SDK=H:\tools"
"H:\tools\mingw1310_64\bin\g++.exe" -std=c++20 -I"H:\naksha-lidar 2\native\naksha_vulkan\include" -I"H:\tools\Include\Volk" -I"H:\tools\Include\vma" -c "H:\naksha-lidar 2\native\naksha_vulkan\src\core\vulkan\VulkanContext.cpp" -o nul 2>&1
