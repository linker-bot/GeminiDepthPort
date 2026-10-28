// SPDX-License-Identifier: Apache-2.0
// GeminiDepthPort: checked owned copies of native packed YUYV frames.
#pragma once
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <vector>

namespace orbbec_camera {
namespace native_yuyv {
inline std::vector<uint8_t> pack(const void *source, std::size_t bytes,
                                 std::size_t width, std::size_t height,
                                 std::size_t trusted_stride = 0) {
  const auto maximum = static_cast<std::size_t>(std::numeric_limits<uint32_t>::max());
  if (!source || !width || !height || width % 2 || width > maximum / 2) {
    throw std::invalid_argument("Invalid native YUYV dimensions or source");
  }
  const auto row = width * 2;
  const auto stride = trusted_stride ? trusted_stride : row;
  if (stride < row || height > maximum / stride || bytes != height * stride) {
    throw std::invalid_argument("Native YUYV buffer layout does not match its declared span");
  }
  std::vector<uint8_t> result(height * row);
  const auto *input = static_cast<const uint8_t *>(source);
  for (std::size_t index = 0; index < height; ++index) {
    std::memcpy(result.data() + index * row, input + index * stride, row);
  }
  return result;
}
}  // namespace native_yuyv
}  // namespace orbbec_camera
