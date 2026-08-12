#include <pebble.h>

#include "view.h"

int main(void) {
  view_init();
  app_event_loop();
  view_deinit();
  return 0;
}
