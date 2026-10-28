// SPDX-License-Identifier: Apache-2.0
// Minimal C++ SDK + shared ROS C ABI reference; NOT the official ROS driver.
#include <libobsensor/ObSensor.hpp>
#include <nlohmann/json.hpp>
#include "gdp_ros_bridge.h"
#include "gdp_ros_aux.h"
#include "gdp_ros_validation.hpp"
#include "gdp_ros_aux_validation.hpp"
#include <algorithm>
#include <array>
#include <chrono>
#include <csignal>
#include <cstring>
#include <dlfcn.h>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

using Json = nlohmann::json;
using Clock = std::chrono::steady_clock;
static volatile std::sig_atomic_t stopped = 0;
static void stop_signal(int) { stopped = 1; }
struct Failure : std::runtime_error {
  using std::runtime_error::runtime_error;
};
static void require(bool ok, const char *code) {
  if (!ok)
    throw Failure(code);
}
static std::string session_id() {
  std::array<unsigned char, 16> bytes{};
  std::ifstream source("/dev/urandom", std::ios::binary);
  require(bool(source.read(reinterpret_cast<char *>(bytes.data()), bytes.size())), "RANDOM_FAILED");
  std::ostringstream result;
  for (auto byte : bytes)
    result << std::hex << std::setfill('0') << std::setw(2) << unsigned(byte);
  return result.str();
}
struct DlCloser {
  void operator()(void *p) const {
    if (p)
      dlclose(p);
  }
};
struct Api {
  std::unique_ptr<void, DlCloser> library;
  decltype(&gdp_ros_create) create;
  decltype(&gdp_ros_publish) publish;
  decltype(&gdp_ros_destroy) destroy;
  decltype(&gdp_ros_set_session_id) session;
  decltype(&gdp_ros_aux_create) aux_create;
  decltype(&gdp_ros_aux_publish_pair) aux_publish;
  decltype(&gdp_ros_aux_destroy) aux_destroy;
  decltype(&gdp_ros_aux_set_session_id) aux_session;
  template <class T> T symbol(const char *name) {
    void *p = dlsym(library.get(), name);
    require(p != nullptr, "BRIDGE_SYMBOL_MISSING");
    T fn;
    static_assert(sizeof(fn) == sizeof(p));
    std::memcpy(&fn, &p, sizeof(fn));
    return fn;
  }
  explicit Api(const std::string &path) : library(dlopen(path.c_str(), RTLD_NOW | RTLD_LOCAL)) {
    require(bool(library), "BRIDGE_LOAD_FAILED");
    require(symbol<decltype(&gdp_ros_abi_version)>("gdp_ros_abi_version")() == 1 &&
                symbol<decltype(&gdp_ros_aux_abi_version)>("gdp_ros_aux_abi_version")() == 1,
            "BRIDGE_ABI_MISMATCH");
    create = symbol<decltype(create)>("gdp_ros_create");
    publish = symbol<decltype(publish)>("gdp_ros_publish");
    destroy = symbol<decltype(destroy)>("gdp_ros_destroy");
    session = symbol<decltype(session)>("gdp_ros_set_session_id");
    aux_create = symbol<decltype(aux_create)>("gdp_ros_aux_create");
    aux_publish = symbol<decltype(aux_publish)>("gdp_ros_aux_publish_pair");
    aux_destroy = symbol<decltype(aux_destroy)>("gdp_ros_aux_destroy");
    aux_session = symbol<decltype(aux_session)>("gdp_ros_aux_set_session_id");
  }
};
struct Handle {
  void *value = nullptr;
  void (*destroy)(void *) = nullptr;
  ~Handle() {
    if (value)
      destroy(value);
  }
  Handle() = default;
  Handle(const Handle &) = delete;
  Handle &operator=(const Handle &) = delete;
};
struct Distortion {
  std::string model;
  std::vector<double> values;
};
static Distortion distortion(const OBCameraDistortion &d) {
  const std::array<double, 8> all{d.k1, d.k2, d.k3, d.k4, d.k5, d.k6, d.p1, d.p2};
  require(std::all_of(all.begin(), all.end(), [](double x) { return std::isfinite(x); }),
          "SDK_DISTORTION_INVALID");
  switch (int(d.model)) {
  case 0:
    require(std::all_of(all.begin(), all.end(), [](double x) { return x == 0; }),
            "SDK_DISTORTION_UNSUPPORTED");
    return {"plumb_bob", std::vector<double>(5, 0)};
  case 3:
    require(d.k4 == 0 && d.k5 == 0 && d.k6 == 0, "SDK_DISTORTION_UNSUPPORTED");
    return {"plumb_bob", {d.k1, d.k2, d.p1, d.p2, d.k3}};
  case 4:
    return {"rational_polynomial", {d.k1, d.k2, d.p1, d.p2, d.k3, d.k4, d.k5, d.k6}};
  case 5:
    require(d.k5 == 0 && d.k6 == 0 && d.p1 == 0 && d.p2 == 0, "SDK_DISTORTION_UNSUPPORTED");
    return {"equidistant", {d.k1, d.k2, d.k3, d.k4}};
  default:
    throw Failure("SDK_DISTORTION_UNSUPPORTED");
  }
}
struct Profile {
  uint32_t width, height, fps;
  std::string frame_id;
};
struct Settings {
  std::string serial, bridge, ns;
  Profile color, depth;
  bool normalize;
  double warmup;
  uint64_t skew;
};
static Settings settings(const Json &j) {
  require(j.at("version") == 1, "CONFIG_VERSION");
  auto profile = [](const Json &p) {
    Profile result{p.at("width").get<uint32_t>(), p.at("height").get<uint32_t>(),
                   p.at("fps").get<uint32_t>(), p.at("frame_id").get<std::string>()};
    require(result.width == 640 && result.height == 480 && result.fps == 30 &&
                gdp_ros::token(result.frame_id),
            "EXACT_640_480_30_REQUIRED");
    return result;
  };
  const auto policy = j.at("depth").at("unit_policy").get<std::string>();
  require(policy == "require-mm" || policy == "normalize-mm", "DEPTH_POLICY_REQUIRED");
  require(j.at("pairing").at("clock") == "device" && j.at("pairing").at("max_delta_us") == 20000,
          "EXPLICIT_DEVICE_20MS_REQUIRED");
  require(j.at("ros").at("stamp_source") == "sdk-system-unix", "SYSTEM_UNIX_STAMP_REQUIRED");
  Settings result{j.at("serial"),
                  j.at("ros").at("bridge"),
                  j.at("ros").at("namespace"),
                  profile(j.at("color")),
                  profile(j.at("depth")),
                  policy == "normalize-mm",
                  j.value("startup_discovery_seconds", 0.0),
                  20000};
  require(!result.serial.empty() && !result.bridge.empty() && result.bridge.front() == '/' &&
              gdp_ros::absolute_name(result.ns, false) &&
              result.color.frame_id != result.depth.frame_id,
          "CONFIG_BINDING_INVALID");
  require(std::isfinite(result.warmup) && result.warmup >= 0 && result.warmup <= 30,
          "WARMUP_LIMIT");
  return result;
}
static std::shared_ptr<ob::VideoStreamProfile>
check_profile(const std::shared_ptr<ob::Frame> &frame, const Profile &expected, OBFormat format) {
  require(bool(frame) && frame->getFormat() == format, "FRAME_FORMAT");
  auto video = frame->as<ob::VideoFrame>();
  require(video->getWidth() == expected.width && video->getHeight() == expected.height &&
              video->getDataSize() == uint64_t(expected.width) * expected.height * 2 &&
              video->getData() != nullptr,
          "FRAME_LAYOUT");
  auto profile = frame->getStreamProfile()->as<ob::VideoStreamProfile>();
  require(profile->getWidth() == expected.width && profile->getHeight() == expected.height &&
              profile->getFps() == expected.fps && profile->getFormat() == format,
          "ACTUAL_PROFILE_MISMATCH");
  return profile;
}
static std::vector<uint8_t> normalize_mm(const uint8_t *source, size_t bytes, float scale,
                                         uint64_t &zeros, uint64_t &clamps) {
  require(source && bytes % 2 == 0 && std::isfinite(scale) && scale > 0, "DEPTH_SCALE_INVALID");
  std::vector<uint8_t> output(bytes);
  uint64_t local_zeros = 0, local_clamps = 0;
  for (size_t i = 0; i < bytes; i += 2) {
    const uint16_t raw = uint16_t(source[i]) | (uint16_t(source[i + 1]) << 8);
    double mm = std::floor(double(raw) * double(scale) + 0.5);
    require(std::isfinite(mm) && mm <= 65535, "DEPTH_NORMALIZATION_OVERFLOW");
    if (!raw) {
      mm = 0;
      local_zeros++;
    } else if (mm < 1) {
      mm = 1;
      local_clamps++;
    }
    const auto value = uint16_t(mm);
    output[i] = uint8_t(value & 255);
    output[i + 1] = uint8_t(value >> 8);
  }
  zeros += local_zeros;
  clamps += local_clamps;
  return output;
}
struct Publishers {
  Handle color, depth, aux;
  Publishers(Api &api, const Settings &s, const std::shared_ptr<ob::VideoStreamProfile> &cp,
             const std::shared_ptr<ob::VideoStreamProfile> &dp, const std::string &session) {
    color.destroy = api.destroy;
    depth.destroy = api.destroy;
    aux.destroy = api.aux_destroy;
    auto create = [&](Handle &handle, const char *name, const Profile &p,
                      const std::shared_ptr<ob::VideoStreamProfile> &profile) {
      auto intrinsic = profile->getIntrinsic();
      require(intrinsic.width == int(p.width) && intrinsic.height == int(p.height),
              "SDK_INTRINSIC_GEOMETRY");
      auto dist = distortion(profile->getDistortion());
      const std::string node = std::string("gdp_cpp_reference_") + name, prefix = s.ns + "/" + name,
                        image = prefix + "/image_raw", info = prefix + "/camera_info",
                        metadata = prefix + "/frame_metadata";
      gdp_ros_config c{};
      c.abi_version = 1;
      c.struct_size = sizeof(c);
      c.node_name = node.c_str();
      c.namespace_name = s.ns.c_str();
      c.image_topic = image.c_str();
      c.camera_info_topic = info.c_str();
      c.metadata_topic = metadata.c_str();
      c.frame_id = p.frame_id.c_str();
      c.width = p.width;
      c.height = p.height;
      c.distortion_model = dist.model.c_str();
      c.d = dist.values.data();
      c.d_len = uint32_t(dist.values.size());
      c.k[0] = intrinsic.fx;
      c.k[2] = intrinsic.cx;
      c.k[4] = intrinsic.fy;
      c.k[5] = intrinsic.cy;
      c.k[8] = 1;
      c.r[0] = c.r[4] = c.r[8] = 1;
      c.p[0] = intrinsic.fx;
      c.p[2] = intrinsic.cx;
      c.p[5] = intrinsic.fy;
      c.p[6] = intrinsic.cy;
      c.p[10] = 1;
      require(gdp_ros::config_valid(&c), "SDK_CALIBRATION_INVALID");
      require(api.create(&c, &handle.value) == 0, "ROS_CREATE_FAILED");
      require(api.session(handle.value, session.c_str()) == 0, "ROS_SESSION_FAILED");
    };
    create(color, "color", s.color, cp);
    create(depth, "depth", s.depth, dp);
    auto e = dp->getExtrinsicTo(cp);
    const std::string node = "gdp_cpp_reference_aux", topic = s.ns + "/rgbd/pair_metadata";
    const auto now = std::chrono::duration_cast<std::chrono::nanoseconds>(
                         std::chrono::system_clock::now().time_since_epoch())
                         .count();
    gdp_ros_aux_config c{};
    c.abi_version = 1;
    c.struct_size = sizeof(c);
    c.node_name = node.c_str();
    c.namespace_name = s.ns.c_str();
    c.pair_metadata_topic = topic.c_str();
    c.parent_frame = s.color.frame_id.c_str();
    c.child_frame = s.depth.frame_id.c_str();
    c.stamp_sec = int32_t(now / 1000000000);
    c.stamp_nanosec = uint32_t(now % 1000000000);
    for (size_t i = 0; i < 9; i++) {
      c.rotation_row_major[i] = e.rot[i];
    }
    for (size_t i = 0; i < 3; i++) {
      c.translation_m[i] = double(e.trans[i]) / 1000.;
    }
    require(gdp_ros::aux_config_valid(&c), "SDK_EXTRINSICS_INVALID");
    require(api.aux_create(&c, &aux.value) == 0, "ROS_AUX_CREATE_FAILED");
    require(api.aux_session(aux.value, session.c_str()) == 0, "ROS_AUX_SESSION_FAILED");
  }
};
static gdp_ros_frame message(const std::shared_ptr<ob::Frame> &frame, const Profile &p, bool depth,
                             bool normalized, float scale, const uint8_t *bytes, size_t size) {
  gdp_ros_frame f{};
  f.abi_version = 1;
  f.struct_size = sizeof(f);
  f.encoding = depth ? GDP_ROS_ENCODING_DEPTH16 : GDP_ROS_ENCODING_YUYV;
  f.width = p.width;
  f.height = p.height;
  f.step = p.width * 2;
  f.sequence = frame->getIndex();
  f.has_device_timestamp = f.has_system_timestamp = 1;
  f.device_timestamp_us = frame->getTimeStampUs();
  f.system_timestamp_us = frame->getSystemTimeStampUs();
  const uint64_t maximum = uint64_t(INT32_MAX) * 1000000 + 999999;
  require(f.system_timestamp_us > 0 && f.system_timestamp_us <= maximum, "ROS_STAMP_RANGE");
  f.stamp_sec = int32_t(f.system_timestamp_us / 1000000);
  f.stamp_nanosec = uint32_t(f.system_timestamp_us % 1000000) * 1000;
  f.has_depth_scale = depth;
  f.normalized = normalized;
  f.source_depth_scale_mm = depth ? scale : 0;
  f.output_depth_scale_mm = depth ? 1 : 0;
  f.data = bytes;
  f.data_len = size;
  require(gdp_ros::frame_valid(&f, p.width, p.height), "ROS_FRAME_CONTRACT");
  return f;
}
struct ActivePipeline {
  std::shared_ptr<ob::Pipeline> value;
  bool started = false;
  ~ActivePipeline() {
    if (started)
      try {
        value->stop();
      } catch (...) {
      }
  }
};
static Json run(const Settings &s, double seconds) {
  require(ob_get_version() == 21006, "SDK_2_10_6_REQUIRED");
  ob::Context::setLoggerSeverity(OB_LOG_SEVERITY_NONE);
  ob::Context context;
  auto device = context.queryDeviceList()->getDeviceBySN(s.serial.c_str());
  ActivePipeline pipeline{std::make_shared<ob::Pipeline>(device)};
  auto config = std::make_shared<ob::Config>();
  config->enableVideoStream(OB_STREAM_COLOR, s.color.width, s.color.height, s.color.fps,
                            OB_FORMAT_YUYV);
  config->enableVideoStream(OB_STREAM_DEPTH, s.depth.width, s.depth.height, s.depth.fps,
                            OB_FORMAT_Y16);
  // Match Rust: SDK constructor's frame-sync attempt and default ANY aggregation;
  // no depth alignment, recommended filters or device-property writes here.
  pipeline.value->start(config);
  pipeline.started = true;
  Api api(s.bridge);
  std::unique_ptr<Publishers> pubs;
  const auto session = session_id();
  uint64_t framesets = 0, accepted = 0, rejected = 0, missing = 0, published = 0, publish_calls = 0,
           warm_frames = 0, warm_rejected = 0, normalized_frames = 0, zeros = 0, clamps = 0,
           normalization_ns = 0, publish_ns = 0;
  std::string last_error;
  gdp_ros_pair last{};
  bool has_last = false;
  uint64_t last_color_ros = 0, last_depth_ros = 0;
  const auto start = Clock::now();
  while (!stopped &&
         std::chrono::duration<double>(Clock::now() - start).count() < seconds + s.warmup) {
    const bool warm = std::chrono::duration<double>(Clock::now() - start).count() < s.warmup;
    auto frames = pipeline.value->waitForFrames(100);
    if (!frames)
      continue;
    framesets++;
    if (warm)
      warm_frames++;
    try {
      auto color = frames->colorFrame();
      auto depth = frames->depthFrame();
      if (!color || !depth) {
        missing++;
        throw Failure("MISSING_FRAME");
      }
      auto cp = check_profile(color, s.color, OB_FORMAT_YUYV);
      auto dp = check_profile(depth, s.depth, OB_FORMAT_Y16);
      if (!pubs)
        pubs = std::make_unique<Publishers>(api, s, cp, dp, session);
      const float scale = depth->getValueScale();
      std::vector<uint8_t> owned;
      const uint8_t *data = static_cast<const uint8_t *>(depth->getData());
      if (s.normalize) {
        auto before = Clock::now();
        owned = normalize_mm(data, depth->getDataSize(), scale, zeros, clamps);
        normalization_ns +=
            std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now() - before).count();
        data = owned.data();
        normalized_frames++;
      } else
        require(scale == 1.f, "RAW_DEPTH_NOT_MM");
      auto c = message(color, s.color, false, false, 0,
                       static_cast<const uint8_t *>(color->getData()), color->getDataSize());
      auto d = message(depth, s.depth, true, s.normalize, scale, data, depth->getDataSize());
      gdp_ros_pair pair{};
      pair.abi_version = 1;
      pair.struct_size = sizeof(pair);
      pair.pair_sequence = framesets;
      pair.color_sequence = c.sequence;
      pair.depth_sequence = d.sequence;
      pair.color_stamp_ns = c.system_timestamp_us * 1000;
      pair.depth_stamp_ns = d.system_timestamp_us * 1000;
      pair.color_timestamp_us = c.device_timestamp_us;
      pair.depth_timestamp_us = d.device_timestamp_us;
      pair.delta_us = pair.color_timestamp_us > pair.depth_timestamp_us
                          ? pair.color_timestamp_us - pair.depth_timestamp_us
                          : pair.depth_timestamp_us - pair.color_timestamp_us;
      pair.clock = 1;
      pair.same_frameset = 1;
      require(gdp_ros::pair_valid(&pair), "PAIR_TIMESTAMP_INVALID");
      require(pair.delta_us <= s.skew, "PAIR_SKEW");
      if (has_last) {
        require(pair.pair_sequence > last.pair_sequence &&
                    pair.color_sequence > last.color_sequence &&
                    pair.depth_sequence > last.depth_sequence &&
                    pair.color_timestamp_us > last.color_timestamp_us &&
                    pair.depth_timestamp_us > last.depth_timestamp_us,
                "PAIR_NOT_ADVANCING");
      }
      last = pair;
      has_last = true;
      if (warm)
        continue;
      require(c.system_timestamp_us > last_color_ros && d.system_timestamp_us > last_depth_ros,
              "ROS_STAMP_NOT_ADVANCING");
      accepted++;
      auto before = Clock::now();
      require(api.publish(pubs->color.value, &c) == 0, "ROS_COLOR_PUBLISH_FAILED");
      publish_calls++;
      require(api.publish(pubs->depth.value, &d) == 0, "ROS_DEPTH_PUBLISH_FAILED");
      publish_calls++;
      require(api.aux_publish(pubs->aux.value, &pair) == 0, "ROS_PAIR_PUBLISH_FAILED");
      publish_calls++;
      publish_ns +=
          std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now() - before).count();
      published++;
      last_color_ros = c.system_timestamp_us;
      last_depth_ros = d.system_timestamp_us;
    } catch (const Failure &error) {
      last_error = error.what();
      if (warm)
        warm_rejected++;
      else
        rejected++;
      if (last_error.rfind("ROS_", 0) == 0)
        break;
    }
  }
  pubs.reset();
  pipeline.value->stop();
  pipeline.started = false;
  const bool ok = rejected == 0 && published > 0;
  return Json{{"version", 1},
              {"kind", "minimal-cpp-sdk-ros-reference"},
              {"ok", ok},
              {"sdk_version", 21006},
              {"framesets", framesets},
              {"pairs_accepted", accepted},
              {"pairs_rejected", rejected},
              {"missing_framesets", missing},
              {"pairs_published", published},
              {"individual_publish_calls", publish_calls},
              {"startup_framesets", warm_frames},
              {"startup_rejected", warm_rejected},
              {"last_error", last_error},
              {"normalized_frames", normalized_frames},
              {"zero_samples", zeros},
              {"clamped_samples", clamps},
              {"normalization_wall_ns", normalization_ns},
              {"publish_wall_ns", publish_ns},
              {"elapsed_seconds", std::chrono::duration<double>(Clock::now() - start).count()},
              {"official_driver", false},
              {"hardware_sync_verified", false},
              {"delivery_verified", false},
              {"explicit_device_property_writes", false},
              {"sdk_default_frame_sync_attempt", true}};
}
static void self_test() {
  uint64_t zeros = 0, clamps = 0;
  const uint8_t source[] = {0, 0, 1, 0, 100, 0};
  auto result = normalize_mm(source, sizeof(source), .1f, zeros, clamps);
  require(result == std::vector<uint8_t>({0, 0, 1, 0, 10, 0}) && zeros == 1 && clamps == 1,
          "SELF_TEST_MM");
  bool overflow = false;
  try {
    const uint8_t raw[] = {255, 255};
    normalize_mm(raw, 2, 2.f, zeros, clamps);
  } catch (const Failure &) {
    overflow = true;
  }
  require(overflow, "SELF_TEST_OVERFLOW");
  std::vector<uint8_t> every(65536 * 2);
  for (size_t i = 0; i < 65536; i++) {
    every[i * 2] = uint8_t(i & 255);
    every[i * 2 + 1] = uint8_t(i >> 8);
  }
  auto identity = normalize_mm(every.data(), every.size(), 1.f, zeros, clamps);
  require(identity == every, "SELF_TEST_ALL_U16_IDENTITY");
  bool odd = false;
  try {
    normalize_mm(source, 1, 1.f, zeros, clamps);
  } catch (const Failure &) {
    odd = true;
  }
  require(odd, "SELF_TEST_ODD_LAYOUT");
  bool scale = false;
  try {
    normalize_mm(source, sizeof(source), 0.f, zeros, clamps);
  } catch (const Failure &) {
    scale = true;
  }
  require(scale, "SELF_TEST_INVALID_SCALE");
  std::cout << Json{{"version", 1},
                    {"kind", "minimal-cpp-reference-self-test"},
                    {"ok", true},
                    {"hardware_verified", false}}
            << '\n';
}
int main(int argc, char **argv) {
  try {
    if (argc == 2 && std::string(argv[1]) == "--self-test") {
      self_test();
      return 0;
    }
    require(argc == 5 && std::string(argv[1]) == "--config" && argv[2][0] == '/' &&
                std::string(argv[3]) == "--seconds",
            "USAGE_CONFIG_SECONDS");
    const double seconds = std::stod(argv[4]);
    require(std::isfinite(seconds) && seconds > 0 && seconds <= 3600, "DURATION_LIMIT");
    std::ifstream file(argv[2]);
    require(bool(file), "CONFIG_READ_FAILED");
    Json config;
    file >> config;
    auto s = settings(config);
    std::signal(SIGINT, stop_signal);
    std::signal(SIGTERM, stop_signal);
    auto report = run(s, seconds);
    std::cout << report << '\n';
    return report.at("ok").get<bool>() ? 0 : 1;
  } catch (const Failure &error) {
    std::cerr << Json{{"version", 1},
                      {"kind", "minimal-cpp-sdk-ros-reference"},
                      {"ok", false},
                      {"error", error.what()}}
              << '\n';
    return 1;
  } catch (...) {
    std::cerr << "{\"version\":1,\"kind\":\"minimal-cpp-sdk-ros-reference\",\"ok\":false,\"error\":"
                 "\"SDK_OR_CONFIG_FAILED\"}\n";
    return 1;
  }
}
