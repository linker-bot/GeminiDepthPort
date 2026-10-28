//! Quality state is evidence about checks, not certification of unseen pixels.
//! No SDK, ROS, host clock, process spawning or device access is used here.
mod engine;
mod mapping;
mod window;
pub use engine::*;
pub use mapping::*;
pub use window::*;
mod metrics;
pub use metrics::*;
