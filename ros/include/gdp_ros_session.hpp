// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <cstddef>
namespace gdp_ros {
inline bool session_valid(const char *s) {
  if (!s) return false;
  for (size_t i=0;i<32;++i) if (!((s[i]>='0'&&s[i]<='9')||(s[i]>='a'&&s[i]<='f'))) return false;
  return s[32]=='\0';
}
}
