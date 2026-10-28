// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <stdexcept>
#include <vector>
namespace gdp_usb {
struct Interface { int number; std::vector<unsigned char> endpoints; };
struct Operations {
    virtual ~Operations() = default;
    virtual int claim(int) = 0;
    virtual int clear(unsigned char) = 0;
    virtual int release(int) = 0;
};
struct Counts { unsigned interfaces = 0; unsigned endpoints = 0; };
inline Counts recover(Operations &ops, const std::vector<Interface> &plan) {
    if(plan.empty()) throw std::runtime_error("USB_RECOVERY_NO_VIDEO_ENDPOINTS");
    std::vector<int> claimed;
    claimed.reserve(plan.size());
    Counts result;
    const char *failure = nullptr;
    for(const auto &item : plan) {
        if(ops.claim(item.number)) { failure = "USB_RECOVERY_CLAIM_FAILED"; break; }
        claimed.push_back(item.number);
    }
    // Acquire the entire scope before clearing anything. Never continue on a busy interface.
    if(!failure) for(const auto &item : plan) {
        for(auto ep : item.endpoints) {
            if(ops.clear(ep)) { failure = "USB_RECOVERY_CLEAR_FAILED"; break; }
            ++result.endpoints;
        }
        if(failure) break;
    }
    for(auto i = claimed.rbegin(); i != claimed.rend(); ++i) {
        if(ops.release(*i)) failure = "USB_RECOVERY_RELEASE_FAILED";
    }
    if(failure) throw std::runtime_error(failure);
    result.interfaces = static_cast<unsigned>(claimed.size());
    return result;
}
}
