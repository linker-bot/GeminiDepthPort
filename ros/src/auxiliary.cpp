// SPDX-License-Identifier: Apache-2.0
#include "gdp_ros_aux.h"
#include "gdp_ros_aux_validation.hpp"
#include "gdp_ros_session.hpp"
#include <rclcpp/rclcpp.hpp>
#include <tf2_msgs/msg/tf_message.hpp>
#include <geometry_msgs/msg/transform_stamped.hpp>
#include <std_msgs/msg/string.hpp>
#include <locale>
#include <memory>
#include <new>
#include <sstream>
#include <stdexcept>
namespace {
struct Aux {
  std::shared_ptr<rclcpp::Context> context;
  std::shared_ptr<rclcpp::Node> node;
  rclcpp::Publisher<tf2_msgs::msg::TFMessage>::SharedPtr tf;
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr pairs,evidence;
  std::string session_id;
  bool published=false;
  gdp_ros_pair last{};
  ~Aux() {
    evidence.reset();pairs.reset();tf.reset();node.reset();
    if(context&&context->is_valid()){try{context->shutdown("auxiliary bridge destroyed");}catch(...){}}
  }
};
}
extern "C" uint32_t gdp_ros_aux_abi_version(void){return GDP_ROS_AUX_ABI_VERSION;}
extern "C" int32_t gdp_ros_aux_create(const gdp_ros_aux_config *c,void **out) {
  if(!out)return 1;
  *out=nullptr;
  try {
    if(!gdp_ros::aux_config_valid(c))return 1;
    auto p=std::make_unique<Aux>();p->context=std::make_shared<rclcpp::Context>();p->context->init(0,nullptr);
    rclcpp::NodeOptions options;options.context(p->context).use_global_arguments(false).start_parameter_services(false).start_parameter_event_publisher(false);
    p->node=std::make_shared<rclcpp::Node>(c->node_name,c->namespace_name,options);
    p->tf=p->node->create_publisher<tf2_msgs::msg::TFMessage>("/tf_static",rclcpp::QoS(rclcpp::KeepLast(1)).reliable().transient_local());
    p->pairs=p->node->create_publisher<std_msgs::msg::String>(c->pair_metadata_topic,rclcpp::QoS(rclcpp::KeepLast(10)).reliable().durability_volatile());
    p->evidence=p->node->create_publisher<std_msgs::msg::String>(std::string(c->pair_metadata_topic)+"/evidence",rclcpp::QoS(rclcpp::KeepLast(10)).reliable().durability_volatile());
    const auto tfq=p->tf->get_actual_qos().get_rmw_qos_profile(),pq=p->pairs->get_actual_qos().get_rmw_qos_profile(),eq=p->evidence->get_actual_qos().get_rmw_qos_profile();
    if(tfq.reliability!=RMW_QOS_POLICY_RELIABILITY_RELIABLE||tfq.durability!=RMW_QOS_POLICY_DURABILITY_TRANSIENT_LOCAL||tfq.history!=RMW_QOS_POLICY_HISTORY_KEEP_LAST||tfq.depth!=1
      ||pq.reliability!=RMW_QOS_POLICY_RELIABILITY_RELIABLE||pq.durability!=RMW_QOS_POLICY_DURABILITY_VOLATILE||pq.history!=RMW_QOS_POLICY_HISTORY_KEEP_LAST||pq.depth!=10
      ||eq.reliability!=RMW_QOS_POLICY_RELIABILITY_RELIABLE||eq.durability!=RMW_QOS_POLICY_DURABILITY_VOLATILE||eq.history!=RMW_QOS_POLICY_HISTORY_KEEP_LAST||eq.depth!=10)throw std::runtime_error("auxiliary actual QoS differs");
    geometry_msgs::msg::TransformStamped transform;
    transform.header.frame_id=c->parent_frame;transform.child_frame_id=c->child_frame;
    transform.header.stamp.sec=c->stamp_sec;transform.header.stamp.nanosec=c->stamp_nanosec;
    transform.transform.translation.x=c->translation_m[0];transform.transform.translation.y=c->translation_m[1];transform.transform.translation.z=c->translation_m[2];
    const auto q=gdp_ros::quaternion(c->rotation_row_major);
    transform.transform.rotation.x=q[0];transform.transform.rotation.y=q[1];transform.transform.rotation.z=q[2];transform.transform.rotation.w=q[3];
    tf2_msgs::msg::TFMessage message;message.transforms.push_back(transform);p->tf->publish(std::move(message));
    *out=p.release();return 0;
  }catch(const std::bad_alloc &){return 3;}catch(...){return 2;}
}
extern "C" int32_t gdp_ros_aux_set_session_id(void *handle,const char *id) {
  try {
    if(!handle||!gdp_ros::session_valid(id))return 1;
    auto &p=*static_cast<Aux *>(handle);
    if(p.published||!p.session_id.empty())return 1;
    p.session_id=id;return 0;
  }catch(...){return 2;}
}
extern "C" int32_t gdp_ros_aux_publish_pair(void *handle,const gdp_ros_pair *pair) {
  if(!handle)return 1;
  try {
    if(!gdp_ros::pair_valid(pair))return 1;
    auto &p=*static_cast<Aux *>(handle);
    if(p.published && !gdp_ros::pair_advances(*pair,p.last))return 1;
    std::ostringstream out;out.imbue(std::locale::classic());
    out<<"{\"version\":1,\"pair_sequence\":"<<pair->pair_sequence<<",\"color_sequence\":"<<pair->color_sequence<<",\"depth_sequence\":"<<pair->depth_sequence
       <<",\"color_stamp_ns\":"<<pair->color_stamp_ns<<",\"depth_stamp_ns\":"<<pair->depth_stamp_ns
       <<",\"color_timestamp_us\":"<<pair->color_timestamp_us<<",\"depth_timestamp_us\":"<<pair->depth_timestamp_us<<",\"delta_us\":"<<pair->delta_us
       <<",\"clock\":\""<<(pair->clock==1?"device":"system")<<"\",\"same_frameset\":true,\"provenance\":\"sdkframeset\",\"hardware_sync_verified\":false,\"publisher_qos_verified\":true";
    if(!p.session_id.empty())out<<",\"session_id\":\""<<p.session_id<<"\"";
    out<<"}";
    std_msgs::msg::String message;message.data=out.str();if(message.data.size()>4096)return 1;p.pairs->publish(std::move(message));p.last=*pair;p.published=true;return 0;
  }catch(const std::bad_alloc &){return 3;}catch(...){return 2;}
}
extern "C" void gdp_ros_aux_destroy(void *handle){try{delete static_cast<Aux *>(handle);}catch(...){}}

extern "C" int32_t gdp_ros_aux_publish_evidence(void *handle,const char *json,size_t length) {
  if(!handle||!json||length==0||length>65536)return 1;
  try {auto &p=*static_cast<Aux *>(handle);if(!p.published)return 1;
    std_msgs::msg::String message;message.data.assign(json,length);
    if(message.data.find('\0')!=std::string::npos)return 1;
    p.evidence->publish(std::move(message));return 0;
  }catch(const std::bad_alloc &){return 3;}catch(...){return 2;}
}
