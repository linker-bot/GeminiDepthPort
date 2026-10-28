// SPDX-License-Identifier: Apache-2.0
// Standard USB endpoint recovery only: no device reset, alt-setting or camera property writes.
#include "recovery.hpp"
#include <libusb-1.0/libusb.h>
#include <iostream>
#include <sys/prctl.h>
#include <unistd.h>
#include <signal.h>
#include <cstdlib>
#include <climits>
#include <map>
#include <memory>
#include <set>
#include <string>
using Device = std::unique_ptr<libusb_device_handle, decltype(&libusb_close)>;
struct UsbOperations : gdp_usb::Operations {
    libusb_device_handle *handle;
    explicit UsbOperations(libusb_device_handle *h) : handle(h) {}
    int claim(int n) override { return libusb_claim_interface(handle, n); }
    int clear(unsigned char ep) override { return libusb_clear_halt(handle, ep); }
    int release(int n) override { return libusb_release_interface(handle, n); }
};
static gdp_usb::Counts execute(const std::string &serial) {
    libusb_context *raw_context = nullptr;
    if(libusb_init(&raw_context)) throw std::runtime_error("USB_RECOVERY_CONTEXT");
    std::unique_ptr<libusb_context, decltype(&libusb_exit)> context(raw_context, libusb_exit);
    libusb_device **raw_list = nullptr;
    auto count = libusb_get_device_list(context.get(), &raw_list);
    if(count < 0) throw std::runtime_error("USB_RECOVERY_ENUMERATION");
    auto free_list = [](libusb_device **p) { libusb_free_device_list(p, 1); };
    std::unique_ptr<libusb_device *, decltype(free_list)> list(raw_list, free_list);
    Device selected(nullptr, libusb_close);
    for(ssize_t i = 0; i < count; ++i) {
        libusb_device_descriptor descriptor{};
        if(libusb_get_device_descriptor(raw_list[i], &descriptor)) throw std::runtime_error("USB_RECOVERY_DESCRIPTOR");
        if(descriptor.idVendor != 0x2bc5) continue;
        libusb_device_handle *raw_handle = nullptr;
        if(libusb_open(raw_list[i], &raw_handle)) throw std::runtime_error("USB_RECOVERY_ACCESS");
        Device handle(raw_handle, libusb_close);
        unsigned char bytes[256];
        int size = libusb_get_string_descriptor_ascii(handle.get(), descriptor.iSerialNumber, bytes, sizeof(bytes));
        if(size < 0) throw std::runtime_error("USB_RECOVERY_SERIAL_READ");
        if(std::string(reinterpret_cast<char *>(bytes), static_cast<size_t>(size)) != serial) continue;
        if(selected) throw std::runtime_error("USB_RECOVERY_AMBIGUOUS_IDENTITY");
        selected = std::move(handle);
    }
    if(!selected) throw std::runtime_error("USB_RECOVERY_DEVICE_NOT_FOUND");
    libusb_config_descriptor *raw_config = nullptr;
    if(libusb_get_active_config_descriptor(libusb_get_device(selected.get()), &raw_config)) throw std::runtime_error("USB_RECOVERY_CONFIGURATION");
    std::unique_ptr<libusb_config_descriptor, decltype(&libusb_free_config_descriptor)> config(raw_config, libusb_free_config_descriptor);
    std::map<int, std::set<unsigned char>> endpoints;
    for(unsigned i = 0; i < config->bNumInterfaces; ++i) {
        const auto &interface = config->interface[i];
        for(int j = 0; j < interface.num_altsetting; ++j) {
            const auto &alt = interface.altsetting[j];
            if(alt.bInterfaceClass != LIBUSB_CLASS_VIDEO || alt.bInterfaceSubClass != 2) continue;
            for(unsigned k = 0; k < alt.bNumEndpoints; ++k) {
                const auto &ep = alt.endpoint[k];
                if((ep.bEndpointAddress & LIBUSB_ENDPOINT_DIR_MASK) == LIBUSB_ENDPOINT_IN
                   && (ep.bmAttributes & LIBUSB_TRANSFER_TYPE_MASK) == LIBUSB_TRANSFER_TYPE_BULK)
                    endpoints[alt.bInterfaceNumber].insert(ep.bEndpointAddress);
            }
        }
    }
    std::vector<gdp_usb::Interface> plan;
    for(const auto &item : endpoints) plan.push_back({item.first, {item.second.begin(), item.second.end()}});
    if(libusb_set_auto_detach_kernel_driver(selected.get(), 1)) throw std::runtime_error("USB_RECOVERY_DRIVER_HANDLING");
    UsbOperations ops(selected.get());
    return gdp_usb::recover(ops, plan);
}
int main(int argc, char **argv) {
    try {
        if(argc != 3 || std::string(argv[1]) != "--parent-pid") throw std::runtime_error("USB_RECOVERY_ARGUMENTS");
        std::string parent_arg(argv[2]);
        if(parent_arg.empty() || parent_arg.size() > 10) throw std::runtime_error("USB_RECOVERY_ARGUMENTS");
        for(char c : parent_arg) if(c < '0' || c > '9') throw std::runtime_error("USB_RECOVERY_ARGUMENTS");
        long parent = std::strtol(argv[2], nullptr, 10);
        if(parent <= 0 || parent > INT_MAX) throw std::runtime_error("USB_RECOVERY_ARGUMENTS");
        if(getppid() != parent || prctl(PR_SET_PDEATHSIG, SIGKILL) != 0 || getppid() != parent)
            throw std::runtime_error("USB_RECOVERY_PARENT");
        char input[257]{};
        std::cin.getline(input, sizeof(input));
        std::string serial(input, static_cast<size_t>(std::cin.gcount()));
        if(serial.empty() || serial.size() > 128 || std::cin.bad() || !std::cin.eof()) throw std::runtime_error("USB_RECOVERY_INPUT");
        for(unsigned char c : serial) if(c < 33 || c > 126) throw std::runtime_error("USB_RECOVERY_INPUT");
        auto counts = execute(serial);
        std::cout << "{\"version\":1,\"complete\":true,\"interfaces\":" << counts.interfaces
                  << ",\"endpoints\":" << counts.endpoints << "}\n";
        return 0;
    } catch(const std::exception &e) {
        // All exceptions above contain fixed codes, never identities or system paths.
        std::cout << "{\"version\":1,\"complete\":false,\"code\":\"" << e.what() << "\"}\n";
        return 1;
    }
}
