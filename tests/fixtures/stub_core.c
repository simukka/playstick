/* A libretro core that emulates nothing, for testing playstick-game.
 *
 * Each frame it draws a 16x16 picture, makes one frame of a constant tone
 * (every sample 1000 on the left and 3000 on the right, so the mono mix is
 * 2000), and copies the joypad bits it was given into its save RAM. The test
 * reads the .srm the frontend writes on exit to see which buttons reached the
 * core, and the PCM the daemon relays to see that the sound came through.
 *
 * Built by the test with the system C compiler; skipped where there is none.
 */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

struct retro_system_info { const char *library_name, *library_version,
  *valid_extensions; bool need_fullpath, block_extract; };
struct retro_game_geometry { unsigned base_width, base_height, max_width,
  max_height; float aspect_ratio; };
struct retro_system_timing { double fps, sample_rate; };
struct retro_system_av_info { struct retro_game_geometry geometry;
  struct retro_system_timing timing; };
struct retro_game_info { const char *path; const void *data; size_t size;
  const char *meta; };

typedef bool (*env_t)(unsigned, void *);
typedef void (*video_t)(const void *, unsigned, unsigned, size_t);
typedef void (*sample_t)(int16_t, int16_t);
typedef size_t (*batch_t)(const int16_t *, size_t);
typedef void (*poll_t)(void);
typedef int16_t (*state_t)(unsigned, unsigned, unsigned, unsigned);

static env_t env; static video_t video; static batch_t batch;
static poll_t poll_cb; static state_t state;
static uint32_t pixels[16 * 16];
static int16_t audio[800 * 2];
static uint8_t sram[8];
static uint32_t frames;

void retro_set_environment(env_t cb) { env = cb; }
void retro_set_video_refresh(video_t cb) { video = cb; }
void retro_set_audio_sample(sample_t cb) { (void)cb; }
void retro_set_audio_sample_batch(batch_t cb) { batch = cb; }
void retro_set_input_poll(poll_t cb) { poll_cb = cb; }
void retro_set_input_state(state_t cb) { state = cb; }
unsigned retro_api_version(void) { return 1; }
void retro_init(void) {}
void retro_deinit(void) {}
void retro_set_controller_port_device(unsigned p, unsigned d) { (void)p; (void)d; }
void retro_reset(void) {}

void retro_get_system_info(struct retro_system_info *info) {
  memset(info, 0, sizeof *info);
  info->library_name = "stub";
  info->library_version = "1";
  info->valid_extensions = "nes";
}

void retro_get_system_av_info(struct retro_system_av_info *av) {
  av->geometry = (struct retro_game_geometry){16, 16, 16, 16, 1.0f};
  av->timing = (struct retro_system_timing){60.0, 48000.0};
}

bool retro_load_game(const struct retro_game_info *game) {
  unsigned fmt = 1; /* XRGB8888 */
  env(10, &fmt);
  return game && game->data && game->size > 0;
}

void retro_unload_game(void) {}

void retro_run(void) {
  poll_cb();
  for (unsigned port = 0; port < 2; port++) {
    int16_t mask = state(port, 1, 0, 256);
    sram[port * 2] = (uint8_t)(mask & 0xFF);
    sram[port * 2 + 1] = (uint8_t)(mask >> 8);
  }
  frames++;
  memcpy(&sram[4], &frames, 4);
  for (int i = 0; i < 16 * 16; i++) pixels[i] = frames;
  video(pixels, 16, 16, 16 * 4);
  for (int i = 0; i < 800; i++) { audio[2 * i] = 1000; audio[2 * i + 1] = 3000; }
  batch(audio, 800);
}

size_t retro_serialize_size(void) { return 0; }
bool retro_serialize(void *d, size_t s) { (void)d; (void)s; return false; }
bool retro_unserialize(const void *d, size_t s) { (void)d; (void)s; return false; }
void *retro_get_memory_data(unsigned id) { return id == 0 ? sram : NULL; }
size_t retro_get_memory_size(unsigned id) { return id == 0 ? sizeof sram : 0; }
unsigned retro_get_region(void) { return 0; }
