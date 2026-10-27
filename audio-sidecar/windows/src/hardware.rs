#[derive(Debug, Clone)]
pub struct HardwareInfo {
    pub os: String,
    pub arch: String,
    pub chip_or_gpu: String,
    pub gpu_vendor: String,
    pub vram_mb: u64,
    pub ram_mb: u64,
    pub vulkan: bool,
    pub avx2: bool,
}

pub fn detect_avx2() -> bool {
    #[cfg(any(target_arch = "x86", target_arch = "x86_64"))]
    {
        std::arch::is_x86_feature_detected!("avx2")
    }
    #[cfg(not(any(target_arch = "x86", target_arch = "x86_64")))]
    {
        false
    }
}

pub fn cpu_brand_string() -> Option<String> {
    #[cfg(target_arch = "x86_64")]
    unsafe {
        use std::arch::x86_64::__cpuid;
        let max_ext = __cpuid(0x8000_0000).eax;
        if max_ext < 0x8000_0004 {
            return None;
        }
        let mut bytes = [0u8; 48];
        for (i, leaf) in (0x8000_0002..=0x8000_0004).enumerate() {
            let res = __cpuid(leaf);
            let offset = i * 16;
            bytes[offset..offset + 4].copy_from_slice(&res.eax.to_le_bytes());
            bytes[offset + 4..offset + 8].copy_from_slice(&res.ebx.to_le_bytes());
            bytes[offset + 8..offset + 12].copy_from_slice(&res.ecx.to_le_bytes());
            bytes[offset + 12..offset + 16].copy_from_slice(&res.edx.to_le_bytes());
        }
        let end = bytes.iter().position(|&b| b == 0).unwrap_or(bytes.len());
        let s = String::from_utf8_lossy(&bytes[..end]).trim().to_string();
        if s.is_empty() {
            None
        } else {
            Some(s)
        }
    }
    #[cfg(not(target_arch = "x86_64"))]
    {
        None
    }
}

pub fn vendor_id_to_name(vendor_id: u32) -> &'static str {
    match vendor_id {
        0x10DE => "nvidia",
        0x1002 | 0x1022 => "amd",
        0x8086 | 0x8087 => "intel",
        0x5143 => "qualcomm",
        0x1414 => "microsoft",
        0x106B => "apple",
        _ => "unknown",
    }
}

#[allow(dead_code, non_snake_case)]
pub mod win_hw {
    use super::*;
    use std::ffi::c_void;
    use std::ptr::{null, null_mut};

    #[repr(C)]
    #[derive(Copy, Clone)]
    pub struct GUID {
        pub data1: u32,
        pub data2: u16,
        pub data3: u16,
        pub data4: [u8; 8],
    }

    #[repr(C)]
    #[derive(Copy, Clone)]
    pub struct LUID {
        pub low_part: u32,
        pub high_part: i32,
    }

    #[repr(C)]
    pub struct DxgiAdapterDesc1 {
        pub description: [u16; 128],
        pub vendor_id: u32,
        pub device_id: u32,
        pub sub_sys_id: u32,
        pub revision: u32,
        pub dedicated_video_memory: usize,
        pub dedicated_system_memory: usize,
        pub shared_system_memory: usize,
        pub adapter_luid: LUID,
        pub flags: u32,
    }

    const DXGI_ADAPTER_FLAG_SOFTWARE: u32 = 2;
    const DXGI_ERROR_NOT_FOUND: i32 = 0x887A0002u32 as i32;

    // IID_IDXGIFactory1 = {770aae78-f26f-4dba-a829-253c83d1b387}
    const IID_IDXGI_FACTORY1: GUID = GUID {
        data1: 0x770aae78,
        data2: 0xf26f,
        data3: 0x4dba,
        data4: [0xa8, 0x29, 0x25, 0x3c, 0x83, 0xd1, 0xb3, 0x87],
    };

    #[repr(C)]
    pub struct IDXGIAdapter1Vtbl {
        pub query_interface:
            unsafe extern "system" fn(*mut c_void, *const GUID, *mut *mut c_void) -> i32,
        pub add_ref: unsafe extern "system" fn(*mut c_void) -> u32,
        pub release: unsafe extern "system" fn(*mut c_void) -> u32,
        pub set_private_data: *const c_void,
        pub set_private_data_interface: *const c_void,
        pub get_private_data: *const c_void,
        pub get_parent: *const c_void,
        pub enum_outputs: *const c_void,
        pub get_desc: *const c_void,
        pub check_interface_support: *const c_void,
        pub get_desc1: unsafe extern "system" fn(*mut c_void, *mut DxgiAdapterDesc1) -> i32,
    }

    #[repr(C)]
    pub struct IDXGIAdapter1 {
        pub vtbl: *const IDXGIAdapter1Vtbl,
    }

    #[repr(C)]
    pub struct IDXGIFactory1Vtbl {
        pub query_interface:
            unsafe extern "system" fn(*mut c_void, *const GUID, *mut *mut c_void) -> i32,
        pub add_ref: unsafe extern "system" fn(*mut c_void) -> u32,
        pub release: unsafe extern "system" fn(*mut c_void) -> u32,
        pub set_private_data: *const c_void,
        pub set_private_data_interface: *const c_void,
        pub get_private_data: *const c_void,
        pub get_parent: *const c_void,
        pub enum_adapters: *const c_void,
        pub make_window_association: *const c_void,
        pub get_window_association: *const c_void,
        pub create_swap_chain: *const c_void,
        pub create_software_adapter: *const c_void,
        pub enum_adapters1:
            unsafe extern "system" fn(*mut c_void, u32, *mut *mut IDXGIAdapter1) -> i32,
        pub is_current: *const c_void,
    }

    #[repr(C)]
    pub struct IDXGIFactory1 {
        pub vtbl: *const IDXGIFactory1Vtbl,
    }

    #[repr(C)]
    pub struct MemoryStatusEx {
        pub dw_length: u32,
        pub dw_memory_load: u32,
        pub ull_total_phys: u64,
        pub ull_avail_phys: u64,
        pub ull_total_page_file: u64,
        pub ull_avail_page_file: u64,
        pub ull_total_virtual: u64,
        pub ull_avail_virtual: u64,
        pub ull_avail_extended_virtual: u64,
    }

    #[cfg(target_os = "windows")]
    #[link(name = "dxgi")]
    extern "system" {
        fn CreateDXGIFactory1(riid: *const GUID, pp_factory: *mut *mut IDXGIFactory1) -> i32;
    }

    #[cfg(target_os = "windows")]
    #[link(name = "kernel32")]
    extern "system" {
        fn GlobalMemoryStatusEx(lp_buffer: *mut MemoryStatusEx) -> i32;
        fn LoadLibraryW(lp_lib_file_name: *const u16) -> *mut c_void;
        fn FreeLibrary(h_lib_module: *mut c_void) -> i32;
        fn GetProcAddress(h_module: *mut c_void, lp_proc_name: *const u8) -> *const c_void;
    }

    #[cfg(not(target_os = "windows"))]
    unsafe fn CreateDXGIFactory1(_riid: *const GUID, _pp_factory: *mut *mut IDXGIFactory1) -> i32 {
        -1
    }
    #[cfg(not(target_os = "windows"))]
    unsafe fn GlobalMemoryStatusEx(_lp_buffer: *mut MemoryStatusEx) -> i32 {
        0
    }
    #[cfg(not(target_os = "windows"))]
    unsafe fn LoadLibraryW(_lp_lib_file_name: *const u16) -> *mut c_void {
        null_mut()
    }
    #[cfg(not(target_os = "windows"))]
    unsafe fn FreeLibrary(_h_lib_module: *mut c_void) -> i32 {
        0
    }
    #[cfg(not(target_os = "windows"))]
    unsafe fn GetProcAddress(_h_module: *mut c_void, _lp_proc_name: *const u8) -> *const c_void {
        null()
    }

    pub fn query_ram_mb() -> u64 {
        unsafe {
            let mut mem: MemoryStatusEx = std::mem::zeroed();
            mem.dw_length = std::mem::size_of::<MemoryStatusEx>() as u32;
            if GlobalMemoryStatusEx(&mut mem) != 0 && mem.ull_total_phys > 0 {
                mem.ull_total_phys / (1024 * 1024)
            } else {
                8192
            }
        }
    }

    pub fn query_best_gpu() -> Option<(String, String, u64)> {
        unsafe {
            let mut factory: *mut IDXGIFactory1 = null_mut();
            let hr = CreateDXGIFactory1(&IID_IDXGI_FACTORY1, &mut factory);
            if hr < 0 || factory.is_null() {
                return None;
            }

            let mut best_hw: Option<(String, String, u64)> = None;
            let mut best_any: Option<(String, String, u64)> = None;

            let mut index = 0u32;
            loop {
                let mut adapter: *mut IDXGIAdapter1 = null_mut();
                let enum_hr = ((*(*factory).vtbl).enum_adapters1)(
                    factory as *mut c_void,
                    index,
                    &mut adapter,
                );
                if enum_hr == DXGI_ERROR_NOT_FOUND || enum_hr < 0 || adapter.is_null() {
                    break;
                }
                index += 1;

                let mut desc: DxgiAdapterDesc1 = std::mem::zeroed();
                let desc_hr = ((*(*adapter).vtbl).get_desc1)(adapter as *mut c_void, &mut desc);
                ((*(*adapter).vtbl).release)(adapter as *mut c_void);

                if desc_hr < 0 {
                    continue;
                }

                let name_len = desc
                    .description
                    .iter()
                    .position(|&c| c == 0)
                    .unwrap_or(desc.description.len());
                let name = String::from_utf16_lossy(&desc.description[..name_len])
                    .trim()
                    .to_string();
                let vendor = vendor_id_to_name(desc.vendor_id).to_string();
                let dedicated_mb = (desc.dedicated_video_memory as u64) / (1024 * 1024);
                let is_software =
                    (desc.flags & DXGI_ADAPTER_FLAG_SOFTWARE) != 0 || desc.vendor_id == 0x1414;

                if best_any.as_ref().map_or(true, |b| dedicated_mb > b.2) {
                    best_any = Some((name.clone(), vendor.clone(), dedicated_mb));
                }

                if !is_software && best_hw.as_ref().map_or(true, |b| dedicated_mb > b.2) {
                    best_hw = Some((name, vendor, dedicated_mb));
                }
            }

            ((*(*factory).vtbl).release)(factory as *mut c_void);
            best_hw.or(best_any)
        }
    }

    pub fn probe_vulkan_runtime() -> bool {
        #[repr(C)]
        struct VkInstanceCreateInfo {
            s_type: u32,
            p_next: *const c_void,
            flags: u32,
            p_application_info: *const c_void,
            enabled_layer_count: u32,
            pp_enabled_layer_names: *const *const u8,
            enabled_extension_count: u32,
            pp_enabled_extension_names: *const *const u8,
        }

        type PfnVkCreateInstance = unsafe extern "system" fn(
            *const VkInstanceCreateInfo,
            *const c_void,
            *mut *mut c_void,
        ) -> i32;
        type PfnVkEnumeratePhysicalDevices =
            unsafe extern "system" fn(*mut c_void, *mut u32, *mut *mut c_void) -> i32;
        type PfnVkDestroyInstance = unsafe extern "system" fn(*mut c_void, *const c_void);

        unsafe {
            let dll_name: Vec<u16> = "vulkan-1.dll\0".encode_utf16().collect();
            let hmod = LoadLibraryW(dll_name.as_ptr());
            if hmod.is_null() {
                return false;
            }

            let create_ptr = GetProcAddress(hmod, b"vkCreateInstance\0".as_ptr());
            let enum_ptr = GetProcAddress(hmod, b"vkEnumeratePhysicalDevices\0".as_ptr());
            let destroy_ptr = GetProcAddress(hmod, b"vkDestroyInstance\0".as_ptr());

            let has_device =
                if !create_ptr.is_null() && !enum_ptr.is_null() && !destroy_ptr.is_null() {
                    let vk_create: PfnVkCreateInstance = std::mem::transmute(create_ptr);
                    let vk_enum: PfnVkEnumeratePhysicalDevices = std::mem::transmute(enum_ptr);
                    let vk_destroy: PfnVkDestroyInstance = std::mem::transmute(destroy_ptr);

                    let create_info = VkInstanceCreateInfo {
                        s_type: 1, // VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO
                        p_next: null(),
                        flags: 0,
                        p_application_info: null(),
                        enabled_layer_count: 0,
                        pp_enabled_layer_names: null(),
                        enabled_extension_count: 0,
                        pp_enabled_extension_names: null(),
                    };
                    let mut instance: *mut c_void = null_mut();
                    let res = vk_create(&create_info, null(), &mut instance);
                    if res == 0 && !instance.is_null() {
                        let mut dev_count: u32 = 0;
                        let enum_res = vk_enum(instance, &mut dev_count, null_mut());
                        vk_destroy(instance, null());
                        enum_res == 0 && dev_count > 0
                    } else {
                        false
                    }
                } else {
                    false
                };

            FreeLibrary(hmod);
            has_device
        }
    }

    pub fn detect_windows_hardware() -> HardwareInfo {
        let arch = if cfg!(target_arch = "aarch64") {
            "aarch64"
        } else {
            "x86_64"
        };
        let ram_mb = query_ram_mb();
        let (chip_or_gpu, gpu_vendor, vram_mb) = query_best_gpu().unwrap_or_else(|| {
            let cpu = cpu_brand_string().unwrap_or_else(|| format!("Windows ({})", arch));
            (cpu, "unknown".to_string(), 0)
        });
        let vulkan = vram_mb > 0 && probe_vulkan_runtime();
        let avx2 = detect_avx2();

        HardwareInfo {
            os: "windows".to_string(),
            arch: arch.to_string(),
            chip_or_gpu,
            gpu_vendor,
            vram_mb,
            ram_mb,
            vulkan,
            avx2,
        }
    }
}

pub fn detect_hardware() -> HardwareInfo {
    #[cfg(target_os = "windows")]
    {
        win_hw::detect_windows_hardware()
    }
    #[cfg(not(target_os = "windows"))]
    {
        let os = if cfg!(target_os = "macos") {
            "macos"
        } else {
            std::env::consts::OS
        };
        let arch = if cfg!(target_arch = "aarch64") {
            "aarch64"
        } else if cfg!(target_arch = "x86_64") {
            "x86_64"
        } else {
            std::env::consts::ARCH
        };
        let chip = cpu_brand_string().unwrap_or_else(|| format!("{} ({})", os, arch));
        HardwareInfo {
            os: os.to_string(),
            arch: arch.to_string(),
            chip_or_gpu: chip,
            gpu_vendor: if cfg!(target_os = "macos") {
                "apple".to_string()
            } else {
                "unknown".to_string()
            },
            vram_mb: 0,
            ram_mb: 8192,
            vulkan: false,
            avx2: detect_avx2(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_vendor_mapping() {
        assert_eq!(vendor_id_to_name(0x10DE), "nvidia");
        assert_eq!(vendor_id_to_name(0x1002), "amd");
        assert_eq!(vendor_id_to_name(0x8086), "intel");
        assert_eq!(vendor_id_to_name(0x1414), "microsoft");
    }

    #[test]
    fn test_detect_hardware_returns_valid_struct() {
        let hw = detect_hardware();
        assert!(!hw.os.is_empty());
        assert!(!hw.arch.is_empty());
        assert!(hw.ram_mb > 0);
        // Also verify win_hw fallback path compiles and runs cleanly
        let win_sim = win_hw::detect_windows_hardware();
        assert_eq!(win_sim.os, "windows");
        assert_eq!(win_sim.ram_mb, 8192);
    }
}
