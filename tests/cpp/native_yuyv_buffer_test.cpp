// SPDX-License-Identifier: Apache-2.0
#include "orbbec_camera/native_yuyv.hpp"
#include <iostream>
#include <limits>
#include <stdexcept>

using orbbec_camera::native_yuyv::pack;
void require(bool value) { if (!value) throw std::runtime_error("Synthetic YUYV check failed"); }
template<typename Function> void rejects(Function function) {
  try { function(); } catch (const std::invalid_argument &) { return; }
  throw std::runtime_error("Malformed YUYV was accepted");
}
int main() {
  std::vector<uint8_t> source{16,128,32,128,64,90,80,240};
  const auto expected = source;
  auto owned = pack(source.data(), source.size(), 2, 2);
  require(owned == expected);
  source.assign(source.size(), 0);
  source.clear(); source.shrink_to_fit();
  require(owned == expected);  // Published storage outlives/rejects SDK buffer reuse.
  const std::vector<uint8_t> padded{16,128,32,128,222,223,64,90,80,240,224,225};
  auto tight = pack(padded.data(), padded.size(), 2, 2, 6);
  require(tight == expected);
  rejects([&] { pack(padded.data(), padded.size(), 2, 2); });  // No guessed stride.
  rejects([&] { pack(expected.data(), expected.size()-1, 2, 2); });
  rejects([&] { pack(expected.data(), expected.size()+1, 2, 2); });
  rejects([&] { pack(nullptr, expected.size(), 2, 2); });
  rejects([&] { pack(expected.data(), expected.size(), 0, 2); });
  rejects([&] { pack(expected.data(), expected.size(), 3, 2); });
  rejects([&] { pack(expected.data(), expected.size(), 2, 0); });
  rejects([&] { pack(expected.data(), expected.size(), 2, 2, 3); });
  rejects([&] { pack(expected.data(), expected.size(), 2, std::numeric_limits<size_t>::max(), 6); });
  rejects([&] { pack(expected.data(), expected.size(), std::numeric_limits<size_t>::max(), 2); });
  std::cout << "Synthetic owned YUYV layout checks passed.\n";
}
