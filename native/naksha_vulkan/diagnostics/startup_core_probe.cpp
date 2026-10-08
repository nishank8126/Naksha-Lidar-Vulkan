#include <windows.h>

int main()
{
    MessageBoxA(
        nullptr,
        "Probe B reached main()",
        "Naksha Vulkan Diagnostic",
        MB_OK
    );
    return 0;
}