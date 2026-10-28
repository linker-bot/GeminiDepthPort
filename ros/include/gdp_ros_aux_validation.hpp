// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "gdp_ros_aux.h"
#include "gdp_ros_validation.hpp"
#include <array>
#include <limits>
namespace gdp_ros {
inline bool aux_config_valid(const gdp_ros_aux_config *c) {
  if (!c || c->abi_version != GDP_ROS_AUX_ABI_VERSION || c->struct_size != sizeof(*c)) return false;
  if (!bounded_string(c->node_name) || !bounded_string(c->namespace_name) || !bounded_string(c->pair_metadata_topic)
      || !bounded_string(c->parent_frame) || !bounded_string(c->child_frame)) return false;
  if (!token(c->node_name) || !token(c->parent_frame) || !token(c->child_frame)
      || std::string(c->parent_frame) == c->child_frame || !absolute_name(c->namespace_name, true)
      || !absolute_name(c->pair_metadata_topic, false)) return false;
  const std::string prefix = std::string(c->namespace_name) == "/" ? "/" : std::string(c->namespace_name) + "/";
  if (std::string(c->pair_metadata_topic).compare(0,prefix.size(),prefix) != 0) return false;
  if (c->stamp_sec < 0 || c->stamp_nanosec >= 1000000000 || (c->stamp_sec == 0 && c->stamp_nanosec == 0)) return false;
  if (!finite(c->rotation_row_major) || !finite(c->translation_m)) return false;
  const auto &r = c->rotation_row_major;
  for (size_t row = 0; row < 3; ++row) for (size_t col = 0; col < 3; ++col) {
    double product = 0;
    for (size_t j = 0; j < 3; ++j) product += r[row*3+j] * r[col*3+j];
    if (!near(product,row == col ? 1. : 0.,1e-5)) return false;
  }
  const double det = r[0]*(r[4]*r[8]-r[5]*r[7])-r[1]*(r[3]*r[8]-r[5]*r[6])+r[2]*(r[3]*r[7]-r[4]*r[6]);
  return near(det,1.,1e-5);
}
inline std::array<double,4> quaternion(const double (&r)[9]) {
  std::array<double,4> q;
  if (r[0]+r[4]+r[8]>0) {
    const double s=std::sqrt(r[0]+r[4]+r[8]+1)*2;
    q={(r[7]-r[5])/s,(r[2]-r[6])/s,(r[3]-r[1])/s,s/4};
  } else if (r[0]>r[4] && r[0]>r[8]) {
    const double s=std::sqrt(1+r[0]-r[4]-r[8])*2;
    q={s/4,(r[1]+r[3])/s,(r[2]+r[6])/s,(r[7]-r[5])/s};
  } else if (r[4]>r[8]) {
    const double s=std::sqrt(1+r[4]-r[0]-r[8])*2;
    q={(r[1]+r[3])/s,s/4,(r[5]+r[7])/s,(r[2]-r[6])/s};
  } else {
    const double s=std::sqrt(1+r[8]-r[0]-r[4])*2;
    q={(r[2]+r[6])/s,(r[5]+r[7])/s,s/4,(r[3]-r[1])/s};
  }
  double norm=0;for(double x:q)norm+=x*x;norm=std::sqrt(norm);for(double &x:q)x/=norm;return q;
}
inline bool pair_valid(const gdp_ros_pair *p) {
  if (!p || p->abi_version != GDP_ROS_AUX_ABI_VERSION || p->struct_size != sizeof(*p)) return false;
  if (p->same_frameset != 1 || p->hardware_sync_verified != 0 || (p->clock != 1 && p->clock != 2)) return false;
  const uint64_t maximum = uint64_t(std::numeric_limits<int32_t>::max()) * 1000000000 + 999999999;
  if (!p->color_stamp_ns || !p->depth_stamp_ns || p->color_stamp_ns > maximum || p->depth_stamp_ns > maximum) return false;
  if (!p->color_timestamp_us || !p->depth_timestamp_us) return false;
  const uint64_t delta = p->color_timestamp_us > p->depth_timestamp_us ? p->color_timestamp_us-p->depth_timestamp_us : p->depth_timestamp_us-p->color_timestamp_us;
  if (delta != p->delta_us) return false;
  if (p->clock == 2 && (p->color_timestamp_us > maximum/1000 || p->depth_timestamp_us > maximum/1000
      || p->color_timestamp_us*1000 != p->color_stamp_ns || p->depth_timestamp_us*1000 != p->depth_stamp_ns)) return false;
  return true;
}
inline bool pair_advances(const gdp_ros_pair &next,const gdp_ros_pair &last) {
  return next.pair_sequence > last.pair_sequence && next.color_sequence > last.color_sequence && next.depth_sequence > last.depth_sequence
      && next.color_stamp_ns > last.color_stamp_ns && next.depth_stamp_ns > last.depth_stamp_ns
      && next.clock == last.clock && next.color_timestamp_us > last.color_timestamp_us && next.depth_timestamp_us > last.depth_timestamp_us;
}
}
