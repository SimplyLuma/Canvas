/* SPDX-License-Identifier: Apache-2.0 */
#include "../compat/luma-window-frame.h"
#include <assert.h>
#include <stdio.h>

int main(void)
{
    const int widths[] = {360, 500, 1024, 1920};
    for (unsigned i = 0; i < sizeof(widths)/sizeof(widths[0]); ++i) {
        for (int decorated = 0; decorated <= 1; ++decorated) {
            LumaWindowFrame f = luma_window_frame(widths[i], 600, LUMA_FRAME_HEADER_HEIGHT, decorated);
            assert(f.content.width == widths[i]);
            assert(f.outer.width - f.content.width == (decorated ? 18 : 0));
            assert(f.outer.height - f.content.y - f.content.height == (decorated ? 9 : 0));
            LumaFrameRect fitted = luma_frame_fit(f.content, 1280, 800);
            assert(fitted.width <= f.content.width && fitted.height <= f.content.height);
            assert(fitted.x >= f.content.x && fitted.y >= f.content.y);
            if (decorated) {
                assert(luma_frame_resize_edge(f, 0, 0, false) == 5);
                assert(luma_frame_resize_edge(f, f.outer.width - 1, 0, false) == 9);
                assert(luma_frame_resize_edge(f, 0, f.outer.height - 1, false) == 6);
                assert(luma_frame_resize_edge(f, f.outer.width - 1, f.outer.height - 1, false) == 10);
                assert(luma_frame_resize_edge(f, f.outer.width / 2, 0, false) == 1);
                assert(luma_frame_resize_edge(f, f.outer.width / 2, f.outer.height - 1, false) == 2);
                assert(luma_frame_resize_edge(f, 0, f.outer.height / 2, false) == 4);
                assert(luma_frame_resize_edge(f, f.outer.width - 1, f.outer.height / 2, false) == 8);
                assert(luma_frame_resize_edge(f, 0, 0, true) == 0);
                assert(luma_frame_resize_edge(f, 30, 80, false) == 0);
                LumaFrameRect controls = luma_frame_controls(f);
                assert(controls.width == 64 && controls.height == 24);
                LumaFrameRect back = luma_frame_back(f);
                assert(back.width == 24 && back.height == 24 && back.y == controls.y);
                assert(controls.x - back.x - back.width == 8);
                assert((luma_frame_controls_pixel(f, controls.x + 32, controls.y + controls.height + 2, true) >> 24) > 0);
                assert(luma_frame_pixel(f, f.content.x - 2, f.content.y + 50, true, false) != LUMA_FRAME_DARK_WINDOW);

                assert(f.outer.width - controls.x - controls.width == 8);
                assert(luma_frame_controls_pixel(f, controls.x + 12, controls.y + 12, true) >> 24 == 255);
            } else {
                assert(luma_frame_resize_edge(f, 0, 0, false) == 0);
            }
            for (int dark = 0; dark <= 1; ++dark) {
                assert(luma_frame_pixel(f, f.content.x + 25, f.content.y + 25, dark, false) == 0);
                assert(luma_frame_pixel(f, -1, 0, dark, false) == 0);
                for (int y = 0; y < f.outer.height; ++y) {
                    for (int x = 0; x < f.outer.width; ++x) {
                        uint32_t pixel = luma_frame_pixel(f, x, y, dark, false);
                        unsigned alpha = pixel >> 24;
                        assert(((pixel >> 16) & 255) <= alpha);
                        assert(((pixel >> 8) & 255) <= alpha);
                        assert((pixel & 255) <= alpha);
                        if (!decorated) assert(pixel == 0);
                    }
                }
            }
        }
    }
    puts("App Kit compatibility frame: widths, content isolation, premultiplied edges, handheld bypass pass");
}
