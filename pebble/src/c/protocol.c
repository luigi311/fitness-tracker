#include <pebble.h>
#include <string.h>

#include "protocol.h"

static PebbleProtocolUpdate s_update;
static void *s_update_context;
static PebbleProtocolState *s_state;
static AppTimer *s_sync_retry_timer;

#define SYNC_RETRY_INITIAL_MS 250
#define SYNC_RETRY_MAX_MS 5000
static uint32_t s_sync_retry_delay_ms = SYNC_RETRY_INITIAL_MS;

static void request_full_state(void);

static bool retryable_sync_failure(AppMessageResult reason) {
  return reason == APP_MSG_BUSY ||
         reason == APP_MSG_NOT_CONNECTED ||
         reason == APP_MSG_SEND_TIMEOUT ||
         reason == APP_MSG_APP_NOT_RUNNING;
}

static void retry_full_state(void *context) {
  (void)context;
  s_sync_retry_timer = NULL;
  request_full_state();
}

static void schedule_full_state_retry(AppMessageResult reason) {
  if (!retryable_sync_failure(reason) || s_sync_retry_timer != NULL) {
    return;
  }
  s_sync_retry_timer = app_timer_register(
      s_sync_retry_delay_ms, retry_full_state, NULL);
  if (s_sync_retry_timer != NULL && s_sync_retry_delay_ms < SYNC_RETRY_MAX_MS) {
    s_sync_retry_delay_ms *= 2;
    if (s_sync_retry_delay_ms > SYNC_RETRY_MAX_MS) {
      s_sync_retry_delay_ms = SYNC_RETRY_MAX_MS;
    }
  }
}

static void outbox_sent(DictionaryIterator *iterator, void *context) {
  (void)iterator;
  (void)context;
  if (s_sync_retry_timer != NULL) {
    app_timer_cancel(s_sync_retry_timer);
    s_sync_retry_timer = NULL;
  }
  s_sync_retry_delay_ms = SYNC_RETRY_INITIAL_MS;
}

static void outbox_failed(DictionaryIterator *iterator,
                          AppMessageResult reason,
                          void *context) {
  (void)iterator;
  (void)context;
  schedule_full_state_retry(reason);
}

static void request_full_state(void) {
  DictionaryIterator *iter;
  AppMessageResult result = app_message_outbox_begin(&iter);
  if (result != APP_MSG_OK) {
    schedule_full_state_retry(result);
    return;
  }
  KEY_SYNC_REQUEST_WRITE(iter, 1);
  dict_write_end(iter);
  result = app_message_outbox_send();
  if (result != APP_MSG_OK) {
    schedule_full_state_retry(result);
  }
}

static void inbox_received(DictionaryIterator *iter, void *context) {
  (void)context;
  PebbleProtocolState *state = s_state;
  Tuple *tuple;

  if (!state) {
    return;
  }

  if ((tuple = dict_find(iter, KEY_UNITS))) {
    state->units = (KEY_UNITS_TUPLE_VALUE(tuple) == 1)
      ? PEBBLE_UNITS_IMPERIAL
      : PEBBLE_UNITS_METRIC;
    state->units_changed = true;
  }

  if ((tuple = dict_find(iter, KEY_HR))) {
    state->last_hr = KEY_HR_TUPLE_VALUE(tuple);
    state->have_hr = true;
  }
  if ((tuple = dict_find(iter, KEY_PACE))) {
    state->last_pace_x100 = KEY_PACE_TUPLE_VALUE(tuple);
    state->have_pace = true;
  }
  if ((tuple = dict_find(iter, KEY_CADENCE))) {
    state->last_cad = KEY_CADENCE_TUPLE_VALUE(tuple);
    state->have_cad = true;
  }
  if ((tuple = dict_find(iter, KEY_DISTANCE))) {
    state->last_dist_m = KEY_DISTANCE_TUPLE_VALUE(tuple);
    state->have_dist = true;
  }
  if ((tuple = dict_find(iter, KEY_POWER))) {
    state->last_power = KEY_POWER_TUPLE_VALUE(tuple);
    state->have_power = true;
  }

  if ((tuple = dict_find(iter, KEY_TGT_KIND))) {
    PebbleTargetKind next_kind =
      (PebbleTargetKind)KEY_TGT_KIND_TUPLE_VALUE(tuple);
    if (state->target_kind == TGT_NONE && next_kind != TGT_NONE &&
        state->have_workout_step) {
      state->step_changed = true;
    }
    state->target_kind = next_kind;
  }
  if ((tuple = dict_find(iter, KEY_TGT_LO))) {
    state->target_lo = KEY_TGT_LO_TUPLE_VALUE(tuple);
  }
  if ((tuple = dict_find(iter, KEY_TGT_HI))) {
    state->target_hi = KEY_TGT_HI_TUPLE_VALUE(tuple);
  }
  if ((tuple = dict_find(iter, KEY_WORKOUT_OUTDOOR))) {
    state->workout_outdoor = KEY_WORKOUT_OUTDOOR_TUPLE_VALUE(tuple) == 1;
  }
  if ((tuple = dict_find(iter, KEY_WORKOUT_STEP))) {
    KEY_WORKOUT_STEP_C_TYPE next_step = KEY_WORKOUT_STEP_TUPLE_VALUE(tuple);
    if (state->target_kind != TGT_NONE && state->have_workout_step &&
        next_step != state->workout_step) {
      state->step_changed = true;
    }
    state->workout_step = next_step;
    state->have_workout_step = true;
  }

  if (s_update) {
    s_update(s_update_context);
  }
}

void pebble_protocol_init(PebbleProtocolState *state) {
  memset(state, 0, sizeof(*state));
  state->units = PEBBLE_UNITS_METRIC;
  state->target_kind = TGT_NONE;
}

void pebble_protocol_start(PebbleProtocolState *state,
                           PebbleProtocolUpdate update,
                           void *context) {
  s_update = update;
  s_update_context = context;
  s_state = state;
  app_message_register_inbox_received(inbox_received);
  app_message_register_outbox_sent(outbox_sent);
  app_message_register_outbox_failed(outbox_failed);
  app_message_open(256, 64);
  request_full_state();
}

void pebble_protocol_stop(void) {
  if (s_sync_retry_timer != NULL) {
    app_timer_cancel(s_sync_retry_timer);
    s_sync_retry_timer = NULL;
  }
  s_sync_retry_delay_ms = SYNC_RETRY_INITIAL_MS;
  app_message_deregister_callbacks();
  s_update = NULL;
  s_update_context = NULL;
  s_state = NULL;
}
