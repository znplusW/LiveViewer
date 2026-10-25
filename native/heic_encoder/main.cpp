// LiveViewer's RGB-to-HEIC subprocess. MIT; external libraries remain shared.
#include <libheif/heif.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>
#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif

static void check(heif_error result) {
  if (result.code != heif_error_Ok)
    throw std::runtime_error(result.message ? result.message : "HEIC operation failed");
}
static heif_error write_data(heif_context*, const void* data, size_t size, void*) {
  if (std::fwrite(data, 1, size, stdout) != size)
    return {heif_error_Encoding_error, heif_suberror_Unspecified, "Output write failed"};
  return {heif_error_Ok, heif_suberror_Unspecified, "OK"};
}
int main(int argc, char** argv) {
#ifdef _WIN32
  _setmode(_fileno(stdin), _O_BINARY);
  _setmode(_fileno(stdout), _O_BINARY);
#endif
  heif_context* context = nullptr;
  heif_image* image = nullptr;
  heif_image_handle* handle = nullptr;
  heif_encoder* encoder = nullptr;
  bool initialized = false;
  int status = 1;
  try {
    if (argc == 2 && std::strcmp(argv[1], "--version") == 0) {
      std::printf("LiveViewer.HeicEncoder libheif %s / Kvazaar\n", heif_get_version());
      return 0;
    }
    if (argc != 4 && argc != 5) throw std::runtime_error("Expected width height quality and RGB stdin");
    const int width = std::stoi(argv[1]), height = std::stoi(argv[2]), quality = std::stoi(argv[3]);
    if (width < 1 || height < 1 || width > 32768 || height > 32768 ||
        static_cast<long long>(width) * height > 100000000 || quality < 1 || quality > 100)
      throw std::runtime_error("Invalid image dimensions or quality");
    const int exif_size = argc == 5 ? std::stoi(argv[4]) : 0;
    if (exif_size < 0 || exif_size > 1048576) throw std::runtime_error("Invalid EXIF size");
    check(heif_init(nullptr));
    initialized = true;
    context = heif_context_alloc();
    if (!context) throw std::runtime_error("Could not allocate HEIC context");
    check(heif_image_create(width, height, heif_colorspace_RGB, heif_chroma_interleaved_RGB, &image));
    check(heif_image_add_plane(image, heif_channel_interleaved, width, height, 8));
    int stride = 0;
    auto* pixels = heif_image_get_plane(image, heif_channel_interleaved, &stride);
    const size_t row_size = static_cast<size_t>(width) * 3;
    if (!pixels || stride < row_size) throw std::runtime_error("Invalid RGB plane");
    for (int row = 0; row < height; ++row)
      if (std::fread(pixels + static_cast<size_t>(row) * stride, 1, row_size, stdin) != row_size)
        throw std::runtime_error("Incomplete RGB input");
    check(heif_context_get_encoder_for_format(context, heif_compression_HEVC, &encoder));
    check(heif_encoder_set_lossy_quality(encoder, quality));
    check(heif_encoder_set_logging_level(encoder, 0));
    check(heif_context_encode_image(context, image, encoder, nullptr, &handle));
    if (exif_size) {
      std::vector<unsigned char> exif(exif_size);
      if (std::fread(exif.data(), 1, exif.size(), stdin) != exif.size())
        throw std::runtime_error("Incomplete EXIF input");
      check(heif_context_add_exif_metadata(context, handle, exif.data(), exif_size));
    }
    heif_writer writer{};
    writer.writer_api_version = 1;
    writer.write = write_data;
    check(heif_context_write(context, &writer, nullptr));
    if (std::fflush(stdout)) throw std::runtime_error("Output flush failed");
    status = 0;
  } catch (const std::exception& error) {
    std::fprintf(stderr, "%s\n", error.what());
  }
  if (handle) heif_image_handle_release(handle);
  if (encoder) heif_encoder_release(encoder);
  if (image) heif_image_release(image);
  if (context) heif_context_free(context);
  if (initialized) heif_deinit();
  return status;
}
