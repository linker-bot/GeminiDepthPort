// SPDX-License-Identifier: Apache-2.0
#include "recovery.hpp"
#include <cassert>
#include <string>
struct Fake : gdp_usb::Operations {
    std::vector<std::string> calls;
    std::string fail;
    int op(std::string s) { calls.push_back(s); return s == fail ? -1 : 0; }
    int claim(int n) override { return op("claim"+std::to_string(n)); }
    int clear(unsigned char n) override { return op("clear"+std::to_string(n)); }
    int release(int n) override { return op("release"+std::to_string(n)); }
};
int main() {
    std::vector<gdp_usb::Interface> plan{{1,{129}},{5,{130}}};
    Fake success; auto result=gdp_usb::recover(success,plan);
    assert(result.interfaces==2 && result.endpoints==2);
    assert((success.calls==std::vector<std::string>{"claim1","claim5","clear129","clear130","release5","release1"}));
    for(const auto &failure : {"claim5","clear129","clear130","release5"}) {
        Fake bad; bad.fail=failure; bool refused=false;
        try { gdp_usb::recover(bad,plan); } catch(const std::runtime_error &) { refused=true; }
        assert(refused);
        assert(bad.calls.back()=="release1");
        if(bad.fail=="claim5") assert((bad.calls==std::vector<std::string>{"claim1","claim5","release1"}));
    }
    Fake empty; bool refused=false;
    try { gdp_usb::recover(empty,{}); } catch(const std::runtime_error &) { refused=true; }
    assert(refused && empty.calls.empty());
}
