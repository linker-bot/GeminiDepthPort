// SPDX-License-Identifier: Apache-2.0
// Uses the official SDK's frame factory only: no Context, Pipeline or USB access.
#include "libobsensor/ObSensor.hpp"
#include "orbbec_camera/native_yuyv.hpp"
#include <algorithm>
#include <stdexcept>
#include <vector>
#include <iostream>
int main() {
  const std::vector<uint8_t> expected{16,128,32,128,64,90,80,240};
  auto *input = new uint8_t[expected.size()];
  std::copy(expected.begin(), expected.end(), input);
  bool destroyed = false;
  auto frame = ob::FrameFactory::createVideoFrameFromBuffer(
      OB_FRAME_COLOR, OB_FORMAT_YUYV, 2, 2, input,
      [&](uint8_t *data) { destroyed = true; delete[] data; }, expected.size(), 4);
  if (frame->getFormat() != OB_FORMAT_YUYV) throw std::runtime_error("SDK format mismatch");
  auto owned = orbbec_camera::native_yuyv::pack(frame->getData(), frame->getDataSize(),
                                              frame->getWidth(), frame->getHeight());
  frame.reset();
  if (!destroyed || owned != expected) throw std::runtime_error("SDK frame lifetime mismatch");
  std::cout << "Official SDK synthetic frame ownership checks passed.\n";
}
