# Changelog

## [0.2.0](https://github.com/fx/reachy/compare/v0.1.0...v0.2.0) (2026-08-25)


### Features

* **bench:** add the benchmark suite, baseline and regression gate ([#16](https://github.com/fx/reachy/issues/16)) ([b369585](https://github.com/fx/reachy/commit/b36958527f579fc501019cf856bdf8c683607779))
* **contracts:** declare the robot link wire types, fixtures and schemas ([#5](https://github.com/fx/reachy/issues/5)) ([41ab85f](https://github.com/fx/reachy/commit/41ab85fc2b89069ce024711effd60dbb6f6d8b25))
* **gaze-control:** complete safety validation and rollout ([#26](https://github.com/fx/reachy/issues/26)) ([9577caa](https://github.com/fx/reachy/commit/9577caaed8cd5b36ca07518d67007a646a8eb910))
* **gaze-control:** establish predictive servo foundations ([#24](https://github.com/fx/reachy/issues/24)) ([97fdf79](https://github.com/fx/reachy/commit/97fdf79b0f48299e877695319633a55c0d2c6467))
* **gaze-control:** integrate coordinated head and body motion ([#25](https://github.com/fx/reachy/issues/25)) ([1d656b5](https://github.com/fx/reachy/commit/1d656b5556a46444127cd24f8deeb62f7908177e))
* **groundstation:** implement the session layer, pipeline and observability ([#7](https://github.com/fx/reachy/issues/7)) ([dd5074f](https://github.com/fx/reachy/commit/dd5074f563e8134630f2478fc6ad2868a4880ad4))
* **ha-satellite:** add the audio, motion and perception ports with their adapters ([#12](https://github.com/fx/reachy/issues/12)) ([f4a1372](https://github.com/fx/reachy/commit/f4a137268085e4216fdc5bf2eb34e9c921b39796))
* **ha-satellite:** add the behaviour layer, settings interface and packaging ([#14](https://github.com/fx/reachy/issues/14)) ([bae48c1](https://github.com/fx/reachy/commit/bae48c16b8faa22bf732a3e2695efbfd4532e06d))
* **ha-satellite:** decode and amplify playback so the robot is audible ([#20](https://github.com/fx/reachy/issues/20)) ([391f4f4](https://github.com/fx/reachy/commit/391f4f4a9b9c429f4cbb4eb1a725bc3478136cda))
* **ha-satellite:** expose speaker volume and boost as Home Assistant controls ([#21](https://github.com/fx/reachy/issues/21)) ([0bff4dc](https://github.com/fx/reachy/commit/0bff4dcf6af1caf2ad5fe12aaf67e945e45a3d49))
* **ha-satellite:** vendor the ESPHome satellite core with per-file provenance ([#4](https://github.com/fx/reachy/issues/4)) ([b1e5f99](https://github.com/fx/reachy/commit/b1e5f99af36a0797896aaa7a4121d01c1390c650))
* **perception:** add YuNet face detection with a parity gate ([#8](https://github.com/fx/reachy/issues/8)) ([7c9d48a](https://github.com/fx/reachy/commit/7c9d48a31608a50f60784c3ab9feec0133ffac29))
* **provisioning:** add Ansible roles with an enforced idempotency gate ([#15](https://github.com/fx/reachy/issues/15)) ([912547d](https://github.com/fx/reachy/commit/912547d6ed588a90f345899d4bca27894fa6e41b))
* **reachyctl:** add deploy, config and app with verified deployment ([#13](https://github.com/fx/reachy/issues/13)) ([31c938c](https://github.com/fx/reachy/commit/31c938c7574bb4f7a4144e0d0d9a6386398431fe))
* **reachyctl:** add doctor and the shared check registry ([#10](https://github.com/fx/reachy/issues/10)) ([ddce32b](https://github.com/fx/reachy/commit/ddce32b80dbb8e741cd24dd2c337e55ade58a52d))
* **reachyctl:** add the shared session client and the probe command ([#9](https://github.com/fx/reachy/issues/9)) ([fd5d7cd](https://github.com/fx/reachy/commit/fd5d7cda24cd0f4d1dc3321e879813e1baedb80a))
* **workspace:** create the uv workspace, toolchain pins and task surface ([#2](https://github.com/fx/reachy/issues/2)) ([585f633](https://github.com/fx/reachy/commit/585f633e19b7784fc4c4b86973b41214f0b859a8))


### Bug Fixes

* **ha-satellite:** run the satellite when the daemon executes its entry module ([#18](https://github.com/fx/reachy/issues/18)) ([6a6c90b](https://github.com/fx/reachy/commit/6a6c90b1c005ba7df187fc25327361c482a96efd))
* **ha-satellite:** run the wake-word models over captured audio ([#19](https://github.com/fx/reachy/issues/19)) ([7aa8d05](https://github.com/fx/reachy/commit/7aa8d05c6f54a991bfa4b563640734b022d2bd47))
* **ha-satellite:** stabilize wake and runtime lifecycle ([#22](https://github.com/fx/reachy/issues/22)) ([50c5476](https://github.com/fx/reachy/commit/50c54765afd1de45a47830fa38d3a54aad89d9c8))


### Documentation

* **gaze-control:** specify predictive coordinated motion ([#23](https://github.com/fx/reachy/issues/23)) ([f21378c](https://github.com/fx/reachy/commit/f21378c816c56b8c542ebf34d39d70c316f7c60e))
* plan the Reachy Mini monorepo with specs and change documents ([#1](https://github.com/fx/reachy/issues/1)) ([f1bbaab](https://github.com/fx/reachy/commit/f1bbaab5f8ab778de215b80ca6ba1fde08e6bcb0))
* **runbooks:** add the setup and operations runbooks and complete the agent docs ([#17](https://github.com/fx/reachy/issues/17)) ([3d2e145](https://github.com/fx/reachy/commit/3d2e145c578e6dd291aa363fbb58b16d79bd9e38))
