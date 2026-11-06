#include <stdio.h>
#include <mpv/client.h>

int main(void)
{
    mpv_handle *player = mpv_create();
    if (!player)
        return 1;
    if (mpv_set_option_string(player, "ao", "null") < 0 ||
        mpv_set_option_string(player, "vid", "no") < 0 ||
        mpv_initialize(player) < 0) {
        mpv_terminate_destroy(player);
        return 1;
    }
    char *version = mpv_get_property_string(player, "mpv-version");
    if (!version) {
        mpv_terminate_destroy(player);
        return 1;
    }
    puts(version);
    mpv_free(version);
    mpv_terminate_destroy(player);
    return 0;
}
