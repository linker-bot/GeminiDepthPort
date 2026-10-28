// SPDX-License-Identifier: Apache-2.0
// Test-only: capture one actual SDK frameset and run the SDK Align filter.
#include <libobsensor/ObSensor.hpp>
#include <nlohmann/json.hpp>
#include <chrono>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <utility>
#include <vector>
#include <sys/stat.h>
#include <thread>
using Json = nlohmann::json;
namespace fs = std::filesystem;
static void require(bool ok, const char *code) { if (!ok) throw std::runtime_error(code); }
static void save(const fs::path &path, const void *data, size_t size) {
    std::ofstream out(path, std::ios::binary); out.write(static_cast<const char *>(data), size);
    out.flush(); require(bool(out), "CAPTURE_WRITE_FAILED");
}
static Json camera(const std::shared_ptr<ob::StreamProfile> &base) {
    auto p = base->as<ob::VideoStreamProfile>(); auto k = p->getIntrinsic(); auto d = p->getDistortion();
    require(p->getWidth()==k.width && p->getHeight()==k.height,"SDK_INTRINSIC_PROFILE_DIMENSIONS");
    Json coefficients; std::string model;
    switch (d.model) {
      case OB_DISTORTION_NONE: model="none"; coefficients=Json::array(); break;
      case OB_DISTORTION_BROWN_CONRADY: model="plumb_bob"; coefficients={d.k1,d.k2,d.p1,d.p2,d.k3}; break;
      case OB_DISTORTION_BROWN_CONRADY_K6: model="rational_polynomial"; coefficients={d.k1,d.k2,d.p1,d.p2,d.k3,d.k4,d.k5,d.k6}; break;
      case OB_DISTORTION_KANNALA_BRANDT4: model="equidistant"; coefficients={d.k1,d.k2,d.k3,d.k4}; break;
      default: throw std::runtime_error("UNSUPPORTED_SDK_DISTORTION");
    }
    return Json{{"width",k.width},{"height",k.height},{"fx",k.fx},{"fy",k.fy},{"cx",k.cx},{"cy",k.cy},{"skew",0.0},
                {"distortion_model",model},{"distortion_coefficients",coefficients}};
}
int main(int argc, char **argv) {
    umask(0077);
    try {
        require(argc==3 || (argc==4 && std::string(argv[3])=="--hold-after-ready"),"CAPTURE_ARGUMENTS");
        const bool hold_after_ready=argc==4; Json config; std::ifstream in(argv[1]); in>>config;
        fs::path target(argv[2]); require(target.is_absolute() && !fs::exists(target),"NEW_PRIVATE_OUTPUT_REQUIRED");
        require(fs::create_directory(target),"CREATE_OUTPUT_FAILED"); fs::permissions(target,fs::perms::owner_all);
        ob::Context::setLoggerToConsole(OB_LOG_SEVERITY_OFF); ob::Context context;
        auto list=context.queryDeviceList(); auto dev=list->getDeviceBySN(config["serial"].get<std::string>().c_str());
        ob::Pipeline pipeline(dev); auto selected=std::make_shared<ob::Config>();
        for (const std::string role : {"color","depth"}) {
            const auto &s=config[role]; auto profiles=pipeline.getStreamProfileList(role=="color"?OB_SENSOR_COLOR:OB_SENSOR_DEPTH);
            selected->enableStream(profiles->getVideoStreamProfile(s["width"],s["height"],role=="color"?OB_FORMAT_YUYV:OB_FORMAT_Y16,s["fps"]));
        }
        const auto pair_clock=config["pairing"]["clock"].get<std::string>();
        require(pair_clock=="device" || pair_clock=="system","PAIR_CLOCK_REQUIRED");
        const auto max_delta=config["pairing"]["max_delta_us"].get<uint64_t>();
        require(max_delta>0,"PAIR_DELTA_REQUIRED");
        pipeline.start(selected); auto started=std::chrono::steady_clock::now(); size_t missing=0,skew=0;
        std::shared_ptr<ob::FrameSet> set;
        while(std::chrono::duration<double>(std::chrono::steady_clock::now()-started).count()<12) {
            auto next=pipeline.waitForFrames(100); if(!next)continue;
            auto c=next->colorFrame();auto d=next->depthFrame();if(!c||!d){missing++;continue;}
            auto ct=pair_clock=="device"?c->getTimeStampUs():c->getSystemTimeStampUs();
            auto dt=pair_clock=="device"?d->getTimeStampUs():d->getSystemTimeStampUs();auto delta=ct>dt?ct-dt:dt-ct;
            if(!ct||!dt||delta>max_delta){skew++;continue;}
            if(std::chrono::duration<double>(std::chrono::steady_clock::now()-started).count()<2)continue;
            set=next;break;
        }
        require(bool(set),"NO_COMPLETE_SDK_FRAMESET");auto c=set->colorFrame();auto d=set->depthFrame();
        require(c->dataSize()==size_t(c->width())*c->height()*2 && d->dataSize()==size_t(d->width())*d->height()*2,"SDK_LAYOUT_NOT_PACKED");
        auto cp=c->getStreamProfile();auto dp=d->getStreamProfile();auto ex=dp->getExtrinsicTo(cp);
        for(const auto &item : {std::make_pair(std::string("color"),cp),std::make_pair(std::string("depth"),dp)}) {
            auto p=item.second->as<ob::VideoStreamProfile>();const auto &wanted=config[item.first];
            require(p->getWidth()==wanted["width"].get<uint32_t>() && p->getHeight()==wanted["height"].get<uint32_t>()
                && p->getFps()==wanted["fps"].get<uint32_t>() && p->getFormat()==(item.first=="color"?OB_FORMAT_YUYV:OB_FORMAT_Y16),"ACTUAL_PROFILE_MISMATCH");
        }
        require(std::isfinite(d->getValueScale()) && d->getValueScale()>0,"SDK_SOURCE_SCALE");
        const char ready[]="SDK_FRAMESET_READY\n";
        save(target/"READY",ready,sizeof(ready)-1);
        if(hold_after_ready) {
            // Test-only crash injection boundary: an actual complete, profile-validated
            // frameset is held; no alignment/property/reset operation is performed.
            std::cout<<"SDK_FRAMESET_READY"<<std::endl;
            for(;;) std::this_thread::sleep_for(std::chrono::seconds(1));
        }
        Json rotation=Json::array();for(float v:ex.rot)rotation.push_back(double(v));
        Json translation=Json::array();for(float v:ex.trans)translation.push_back(double(v)/1000.0);
        uint16_t host=1;require(*reinterpret_cast<unsigned char*>(&host)==1,"REFERENCE_REQUIRES_LITTLE_ENDIAN");
        std::vector<unsigned char> source(static_cast<unsigned char*>(d->data()),static_cast<unsigned char*>(d->data())+d->dataSize());
        save(target/"depth.raw16",source.data(),source.size());save(target/"color.yuyv",c->data(),c->dataSize());
        Json descriptor{{"version",1},{"kind","sdk-align-comparison-input"},{"association","sdk_frameset"},{"hardware_sync_verified",false},
            {"physical_accuracy_verified",false},{"color",camera(cp)},{"depth",camera(dp)},{"rotation_row_major",rotation},{"translation_m",translation},
            {"depth_stride",d->width()*2},{"depth_endian","little"},{"source_scale_mm",double(d->getValueScale())},
            {"color_sequence",c->getIndex()},{"depth_sequence",d->getIndex()},{"color_device_us",c->getTimeStampUs()},{"depth_device_us",d->getTimeStampUs()},
            {"color_system_us",c->getSystemTimeStampUs()},{"depth_system_us",d->getSystemTimeStampUs()},{"missing_framesets_before_selection",missing},{"skew_rejected_before_selection",skew},
            {"selected_clock",pair_clock},{"selected_color_timestamp_us",pair_clock=="device"?c->getTimeStampUs():c->getSystemTimeStampUs()},
            {"selected_depth_timestamp_us",pair_clock=="device"?d->getTimeStampUs():d->getSystemTimeStampUs()},{"max_pair_delta_us",max_delta},
            {"sdk_align_match_target_resolution",true},{"sdk_aligned_z_semantics","sdk-reported-not-physically-certified"}};
        ob::Align align(OB_STREAM_COLOR);align.setMatchTargetResolution(true);auto processed=align.process(set);
        require(bool(processed),"SDK_ALIGN_EMPTY");auto aligned=processed->as<ob::FrameSet>()->depthFrame();require(bool(aligned),"SDK_ALIGN_NO_DEPTH");
        require(aligned->width()==c->width() && aligned->height()==c->height() && aligned->dataSize()==size_t(c->width())*c->height()*2,"SDK_ALIGN_LAYOUT");
        require(source.size()==d->dataSize() && std::memcmp(source.data(),d->data(),source.size())==0,"SDK_ALIGN_MUTATED_SOURCE");
        require(std::isfinite(aligned->getValueScale()) && aligned->getValueScale()>0,"SDK_ALIGNED_SCALE");
        auto aligned_profile=aligned->getStreamProfile();
        descriptor["target"]=camera(aligned_profile);
        descriptor["target_grid"]="sdk-aligned-output-profile";
        descriptor["original_depth_to_color_extrinsics"]={{"rotation_row_major",rotation},{"translation_m",translation}};
        auto target_extrinsic=dp->getExtrinsicTo(aligned_profile);
        Json target_rotation=Json::array();for(float v:target_extrinsic.rot)target_rotation.push_back(double(v));
        Json target_translation=Json::array();for(float v:target_extrinsic.trans)target_translation.push_back(double(v)/1000.0);
        descriptor["rotation_row_major"]=target_rotation;descriptor["translation_m"]=target_translation;
        descriptor["target_transform_source"]="depth-profile-to-sdk-aligned-profile";
        save(target/"sdk-aligned.raw16",aligned->data(),aligned->dataSize());descriptor["sdk_aligned_scale_mm"]=double(aligned->getValueScale());
        auto text=descriptor.dump();save(target/"descriptor.json",text.data(),text.size());pipeline.stop();
        const char complete[]="actual SDK frameset and SDK aligned result captured\n";save(target/"CAPTURE_COMPLETE",complete,sizeof(complete)-1);
        std::cout<<Json{{"version",1},{"ok",true},{"scope","SDK-algorithm-comparison-not-physical-accuracy"}}<<std::endl;
    } catch(...) {std::cerr<<"SDK_ALIGN_CAPTURE_FAILED\n";return 1;}
}
