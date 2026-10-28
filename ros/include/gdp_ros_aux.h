// SPDX-License-Identifier: Apache-2.0
#ifndef GDP_ROS_AUX_H
#define GDP_ROS_AUX_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
#define GDP_ROS_AUX_ABI_VERSION 1u
typedef struct gdp_ros_aux_config {
  uint32_t abi_version,struct_size;
  const char *node_name,*namespace_name,*pair_metadata_topic,*parent_frame,*child_frame;
  int32_t stamp_sec;
  uint32_t stamp_nanosec;
  double rotation_row_major[9],translation_m[3];
} gdp_ros_aux_config;
typedef struct gdp_ros_pair {
  uint32_t abi_version,struct_size;
  uint64_t pair_sequence,color_sequence,depth_sequence;
  uint64_t color_stamp_ns,depth_stamp_ns,color_timestamp_us,depth_timestamp_us,delta_us;
  uint32_t clock,same_frameset,hardware_sync_verified;
} gdp_ros_pair;
uint32_t gdp_ros_aux_abi_version(void);
int32_t gdp_ros_aux_create(const gdp_ros_aux_config *config,void **out_handle);
int32_t gdp_ros_aux_set_session_id(void *handle, const char *session_id);
int32_t gdp_ros_aux_publish_pair(void *handle,const gdp_ros_pair *pair);
void gdp_ros_aux_destroy(void *handle);
#ifdef __cplusplus
}
#endif
#endif
