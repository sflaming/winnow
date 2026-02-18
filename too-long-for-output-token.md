It clears `preview_pending` AND stops the timer! So if ANY row highlight event fires AFTER the "Render preview" button is clicked but BEFORE the debounce timer fires, the pending preview is cancelled!

  But wait, the message shown would be "Auto-preview is off...", not "Rendering previews…". So the user wouldn't see "Rendering previews…" frozen.

  Unless... the race happens differently. What if:
  1. User clicks "Render preview"
  2. `_request_preview_render` shows "Rendering previews…" and starts timer
  3. Timer fires (0.18s later), `_start_pending_preview` starts worker
  4. Worker is running (preview_inflight = True)
  5. User navigates to another row
  6. `on_data_table_row_highlighted` → `_request_preview_render(row, force=False)`
  7. `preview_generation` incremented, `preview_pending` cleared, timer stopped
  8. Message set to "Auto-preview is off..."

  Now the worker finishes and calls `_update_preview_if_current` with the old generation number. Since the generation has changed, it tries to start a pending preview, but `preview_pending` is None so nothing happens. The user sees the
  "Auto-preview is off..." message instead of the rendered content.

  Now I'm wondering if the issue is actually when auto-preview is ON and the user navigates quickly. Let me trace through that scenario: the user enables auto-preview, which triggers a render request, then immediately navigates to a
  different row before the initial render completes. This would increment the generation counter and set up a new pending preview with a fresh timer, potentially leaving the first render orphaned.

  When the old worker finishes, it checks the generation and discards its result, then starts processing the new pending preview. The new timer might fire after that, but since the pending preview was already cleared and a worker is now
   inflight, it just returns without doing anything. This flow seems sound, but I'm struggling to pinpoint where the actual freeze could be happening.

  Let me look at `_request_preview_render` again more carefully. The function increments the generation counter, handles the case where no row is selected or auto-preview is disabled, then stores the pending preview request with the
  current generation number and updates the UI to show a rendering message.

  Scheduling the preview start...

  Now I'm tracing through what happens when auto-preview is on and the user switches rows while a worker is running. The key insight is that `preview_generation` gets incremented every time a new preview is requested, so if another
  event like `_refresh_matches_table` fires between when the worker starts and when it finishes, the generation number will have changed, causing the update to be discarded as stale.

  But then `_start_pending_preview` gets called again, which either starts a new worker if there's a pending request, or returns early if auto-preview was turned off. The state machine seems sound—I'm wondering if this is actually a
  Textual-specific issue with how timers or async operations interact.

  Let me think about what "freezes up saying Rendering Previews" actually means. If the preview text never updates from that message, it could mean the worker never completes, the callback never fires, or something else is blocking. I'm
   checking a simpler case now—what happens if the user tries to render a preview when there are no matches loaded?

  Looking at the code flow, if there are no matches, `row` stays None and the function returns early with a message. That's fine. And if the preview tools aren't installed, the worker completes normally and shows an unavailable message.
   But I'm realizing there might be a threading issue—if `_format_preview_block` is called from the worker thread while the main thread is also accessing `self.preview_block_cache`, that could cause problems.

  Let me trace where the cache is actually accessed. The cache gets read and written from `_format_preview_block` in the worker thread, but checking the main thread side, `action_clear` and `_invalidate_preview_pipeline` don't touch the
   cache at all, so there's no concurrent modification happening there.

  Though multiple worker threads could theoretically hit the cache simultaneously, Python's GIL should protect individual dictionary operations. Let me consider a different angle—what if `_refresh_matches_table` gets called while a
  preview is rendering, clearing and rebuilding the matches, and the preview worker ends up with a stale reference to a `MatchRow` object? Actually, that shouldn't matter since the frozen dataclass still points to valid file paths even
  if the table gets rebuilt.

  I'm going to trace through the full auto-preview flow again from the beginning.

  Wait, I just spotted something. Looking at `_refresh_matches_table`, it calls `_request_preview_render` in multiple places—from the scan worker completion, selection changes, row selection, and bulk actions. Each call triggers a
  preview render if auto-preview is enabled, which means the timer gets scheduled repeatedly. This could be the issue.

  When the user presses Enter on a row, `_refresh_matches_table` clears and rebuilds the table. During that rebuild, Textual might be firing `RowHighlighted` events synchronously for each row being added, and each of those events also
  calls `_request_preview_render`. So I'm getting multiple timer resets happening in quick succession—one from each highlighted event during the rebuild, plus another from `_refresh_matches_table` itself. The last timer to fire wins,
  but I need to understand whether Textual emits these events synchronously or defers them.

  Actually, let me step back and think about this differently. What if the real issue is something else entirely?

  I'm looking at the CSS styling for the preview panel and how it's defined as a Static widget. When `_set_preview()` calls `update()` on that widget, it should automatically trigger a repaint and display the new content. But what if
  the panel itself isn't being rendered properly—maybe it's too small, hidden by CSS, or the update isn't actually causing a visual refresh?

  Let me trace through the layout: the preview column is set to 30% width with the Static widget inside it, so it should be visible. But I'm realizing the user said the app "freezes up saying Rendering Previews"—maybe they don't mean
  the app is actually frozen, but rather something else is happening.

  Actually, if the preview panel is stuck showing "Rendering previews…" and never updates, that points to the worker callback not completing. The callback should always set `preview_inflight` to False, and if there's a generation
  mismatch, it would trigger `_start_pending_preview` to queue up the next render. But if there's no pending preview queued, the message just stays there indefinitely. The issue might be that the pending preview state isn't being
  tracked correctly when the callback fires. was cleared (by a subsequent `_request_preview_render` call with auto-preview off), then the "Rendering previews…" text is replaced by "Auto-preview is off...". So case 4 would show the
  correct auto-preview-off message, not "Rendering previews…".

  For case 1: If `preview_inflight` is stuck True, it means a previous worker's `_update_preview_if_current` was never called. This could happen if the worker crashed without reaching the `call_from_thread` line. But the try/except in
  `_preview_worker` should handle exceptions.

  Unless the worker thread itself is killed or the `run_worker` mechanism fails. Textual's `run_worker` should handle this properly.

  For case 2: subprocess has timeouts. The computation is not CPU-intensive (just string formatting). So hanging is unlikely.

  For case 3: `call_from_thread` is Textual's thread-safe mechanism and should work reliably. But what if `preview_inflight` is already True from a previous incomplete render? When the user clicks "Render preview", the timer fires and
  `_start_pending_preview` sees `preview_inflight` is True, so it returns early without starting a new preview. be stuck at "Rendering previews…". The user could theoretically clear this by pressing "Clear" or toggling auto-preview, but
   if the very first preview render fails silently with the worker crashing before it reaches the callback, `preview_inflight` would stay True and lock out all subsequent render attempts. The worker has error handling that wraps the
  formatting logic...

  Still writing worker code... raises an exception, the worker would fail without ever calling `_update_preview_if_current`. This is unlikely under normal circumstances since `call_from_thread` should only fail if the app is shutting
  down, but it's a potential edge case. Let me think about a different failure mode—what if `_format_preview_panel` hangs? It chains through several function calls: `_format_preview_block` for both SD and Drive, which calls
  `render_preview_ascii`, which calls `resolve_preview_image`. For RAW files, this eventually reaches `_extract_preview_with_exiftool`, which...

  Extracting preview with exiftool... I'm checking the `_extract_preview_with_rawpy` function, which tries to import rawpy and returns early if it's unavailable, otherwise processes the RAW file with rawpy.imread and postprocess calls
  that shouldn't hang unless there's a filesystem issue. Let me step back and look at what's actually suspicious in the code rather than continuing to speculate about timeouts.

  The user is on the `feature-preview` branch with staged changes to `photo_dupe.py` that presumably add the preview feature. When "Render preview" is clicked, it triggers a worker that should complete and then call the update callback
  on the main thread to set `preview_inflight = False`. Let me trace through what happens on the first call to see where the logic might break down.

  The issue could be that the timer isn't firing if the main thread is blocked by the scan completion work—rebuilding clusters, refreshing formats, and populating the matches table all happen synchronously, which might delay the timer
  from executing. I need to figure out if that's actually the bottleneck or if something else is preventing the preview from rendering. I need to look at this from a very practical perspective. Let me imagine I'm the user. I:
  1. Open the app
  2. Set drive and SD paths
  3. Click "Scan"
  4. Scan completes, matches are shown
  5. I see a match and want to preview it
  6. I click "Render preview"
  7. The panel says "Rendering previews…" and never changes

  For this to happen, either:
  A. The debounce timer never fires `_start_pending_preview`
  B. `_start_pending_preview` doesn't start the worker because `preview_inflight` is True
  C. The worker hangs
  D. `_update_preview_if_current` is never called or discards the result

  For case A: This would mean Textual's `set_timer` is broken, which is unlikely.

  For case B: `preview_inflight` could be stuck True from a previous preview that never finished. But on first preview it should be False. Actually, I'm realizing there's a potential issue with how `preview_inflight` is defined as a
  class-level attribute—when you assign to `self.preview_inflight`, it shadows the class variable, but if the app gets instantiated multiple times or there's some other edge case, this could cause problems.

  But more likely, during the 0.18s debounce window, if the user clicks on different rows rapidly, each RowHighlighted event calls `_request_preview_render` again, which resets the timer. So the timer keeps getting pushed back and never
   actually fires.

  Actually, wait—I'm seeing a potential issue here. The `on_data_table_row_highlighted` handler is checking if `_match_from_row_key` returns a valid row, but if it returns None, we'd be passing None to `_request_preview_render`. Let me
  trace through what happens in that case.

  Looking at the `_request_preview_render` method, if the row is None, it clears the pending preview and stops the timer entirely. So if `_refresh_matches_table` triggers a final event with a None row, it could cancel the pending
  preview that was just queued from the refresh operation itself. The question is whether `_match_from_row_key` would actually return None for the last event—it converts the row key to an index and looks it up, so it depends on whether
  that lookup succeeds.

  During `table.clear()`, the cursor might move and fire a `RowHighlighted` event with an invalid key, causing `_match_from_row_key` to return None and clearing everything. But then as `_refresh_matches_table` adds rows back, the first
  new row would trigger another `RowHighlighted` event, calling `_request_preview_render` again with the actual row. If auto-preview is enabled, this starts a fresh timer and pending preview; if not, it just shows the disabled message.

  The real issue is whether these `RowHighlighted` events fire synchronously (immediately clearing the pending preview before more rows are added) or asynchronously (allowing `_refresh_matches_table` to finish adding rows first). If
  they're synchronous, the pending preview could get wiped out mid-refresh, and then a new one gets set at the end. If they're asynchronous, the timing becomes unpredictable. setting pending and starting the timer in step 4, but then
  the deferred RowHighlighted event from step 2 fires with None and clears the pending while also updating the preview text—so the user would see the new preview message, not the "Rendering previews…" message stuck forever. Let me trace
   through this more carefully to see if there's actually a race condition here.

  Actually, I just realized I should look at what `run_worker` does with threads and the `exclusive` parameter...

  Configuring the worker call... The worker thread renders the preview and then uses `call_from_thread` to schedule the callback on the main thread, which checks if this generation is still current before updating the display.

  Now I'm thinking about the `group="preview"` parameter—with `exclusive=True`, starting a new worker should automatically cancel the previous one in that group, so there's no need for explicit `cancel_group` calls. Let me trace through
   what happens when a new preview request comes in while one is already running.

  When the user clicks "Render preview" after a scan completes, the flow goes: scan finishes, `done()` runs, then `_refresh_matches_table()` clears and repopulates the table (which might trigger multiple `RowHighlighted` events), and
  finally calls `_request_preview_render()` with `force=False`. Since auto-preview is off by default, this just shows the "Auto-preview is off..." message and returns.

  Then when the user explicitly clicks "Render preview", `action_render_preview()` sets `preview_focus_row` and calls `_request_preview_render()` with `force=True`, which increments the generation counter, sets `preview_pending` to
  track the current row and generation, and schedules a timer for 0.18 seconds later. When that timer fires, `_start_pending_preview()` kicks off the actual preview rendering.

  At that point, the preview worker thread starts running `_preview_worker()`, which formats the preview panel content and sends it back to the main thread via `call_from_thread()`. The main thread then receives the callback in
  `_update_preview_if_current()`, which checks if this generation is still current before updating the display.

  Now I'm tracing through what happens if the user navigates to a different row during the debounce window. When `on_data_table_row_highlighted` fires with the new row, it calls `_request_preview_render()` with force=False. Since
  auto-preview is off, this clears the pending preview and stops the timer entirely. So by the time step 7 would have fired, the timer's already been cancelled and the panel shows "Auto-preview is off..." instead of the rendered
  content. Wait! But actually, step 6.5 happens on the main thread. Step 6 set a timer. The timer fires after 0.18s. If step 6.5 happens before the timer fires (within 0.18s), and auto-preview is off:

  This clears the pending, stops the timer, and shows "Auto-preview is off...". The timer from step 6 is stopped, so `_start_pending_preview` never runs.

  But the preview text would be "Auto-preview is off...", not "Rendering previews…". So the user wouldn't see "Rendering previews…" as a frozen message. They'd see "Auto-preview is off...".

  Unless the events from `on_data_table_row_highlighted` happen before the `_set_preview("Rendering previews…")` call in step 5, but that can't happen since step 5 is synchronous on the main thread.

  Now let me think through what happens when auto-preview is actually on. The user turns it on and navigates between rows, so each row highlight triggers `_request_preview_render` with force=False. Since auto-preview is enabled, each
  call sets pending, displays "Rendering previews…", and schedules a debounce timer.

  The debounce timer resets with each navigation event, so after the user stops navigating, the timer finally fires and the worker starts rendering. This should work fine unless the user navigates so fast the worker never gets a chance
  to run, but eventually they'd stop and the last timer would fire.

  I'm wondering if there's a bug where `on_data_table_row_highlighted` fires repeatedly even when the user isn't actively navigating—maybe if `_refresh_matches_table` is called in a loop? Let me trace through: `_refresh_matches_table`
  isn't called from `on_data_table_row_highlighted`, so there's no direct recursion. But `on_data_table_row_selected` calls `_refresh_matches_table`, which rebuilds the table and might trigger RowHighlighted events.

  Now I'm thinking about a different scenario: what if `_start_pending_preview` is called from the timer callback, but by that time `preview_pending` has already been cleared by a subsequent `_request_preview_render` call when
  auto-preview is off? Let me trace through the sequence: user clicks "Render preview", setting pending to the row with generation 5 and starting the timer. Within 0.18 seconds, the user presses an arrow key, which triggers
  `on_data_table_row_highlighted` and calls `_request_preview_render` with the new row and auto-preview off, incrementing the generation to 6.

  Since auto-preview is off, pending gets set to None and the timer is stopped, so `_start_pending_preview` never actually runs—the preview just shows "Auto-preview is off..." instead of "Rendering previews…", which isn't the bug the
  user is describing.

  But what if the timer fires before the arrow key is pressed? Then the worker starts running with the original row while the user is still interacting with the table, and when they press the arrow key afterward,
  `_request_preview_render` gets called with the new row. Looking at the scenario where the user clicks "Render preview" and waits—the timer fires after 0.18 seconds, triggering the worker to format the preview blocks by calling the
  subprocess for each path. The subprocess has a 4-second timeout, so if it hangs, the preview remains stuck on "Rendering previews…" until that timeout expires.

  For a RAW file specifically, the worst case is brutal: exiftool can take up to 8 seconds across multiple tag attempts, then rawpy's postprocessing runs without a timeout and could hang indefinitely, followed by chafa's 8-second
  attempts for ASCII rendering. With two files to process, this could easily stretch past 30 seconds, and rawpy is the real wildcard since it has no timeout protection.

  The user's mention of "freezes" suggests either the process never completes or takes an extremely long time. If chafa and exiftool aren't installed, things return quickly with an unavailable message. But if chafa is installed and the
  subprocess hangs—maybe trying to access a display—the 4-second timeout eventually fires and retries with a different command, adding another 4 seconds per file.

  So the user might just be seeing a long delay rather than a true freeze. But they said it "doesn't work", implying it never finishes at all. Let me shift focus and look for an actual code bug in the worker function instead of a logic
  issue.

  Examining the worker implementation...

  Checking the preview formatting chain...

  Tracing through the cache lookup and ASCII rendering...

  Examining the preview extraction methods...

  Wait, I'm noticing something in the `resolve_preview_image` function for RAW files — it tries extracting with exiftool first, then falls back to rawpy if that fails, both writing to the same cached path.

  The cached file is a temporary Path that gets populated by these extraction methods, and if successful, that cached file gets passed to `render_preview_ascii` which runs chafa on it. The subprocess has a timeout so it shouldn't hang,
  but I'm wondering if there's a risk if the cached file isn't a valid JPEG — though the timeout should protect against that scenario.

  Now looking at `_extract_preview_with_rawpy`, I notice it doesn't have a timeout built in. The function imports rawpy and Pillow, then uses rawpy to decode the raw image file, postprocesses it with camera white balance settings, and
  saves the result as a JPEG to the output path.

  The rawpy operations themselves don't have timeouts, so if the file is large or the system is slow, this could take a while. But it shouldn't hang indefinitely—the user saying it "doesn't work" suggests it's not completing at all.

  Wait, let me look at `_start_pending_preview` more carefully...

  Examining the preview timer logic...

  I see it now—the issue is in how `_update_preview_if_current` handles the generation check. When a new preview is requested while one is in flight, it sets `preview_inflight` to False and checks if the generation matches. If it
  doesn't match, it calls `_start_pending_preview` to process the pending one, but there's a potential race condition where the pending preview might not get properly queued or executed if multiple rapid requests come in.

  Actually, looking at this more carefully, the real concern is what happens if the worker completes so quickly—before `run_worker` even returns—that the callback fires and tries to start another worker while we're still in the middle
  of the first `run_worker` call. This could cause state to get tangled if `preview_inflight` and `preview_pending` aren't being managed atomically.

  Let me reconsider what the user might actually be experiencing that differs from my mental model here.

  I'm wondering if the real issue is that `_format_preview_block` is being called from a worker thread and accessing `preview_block_cache`, which is a class-level attribute shared across all instances. When the worker thread modifies it
   with operations like `move_to_end()` or direct assignment, it's mutating shared state without synchronization, which could cause race conditions or unexpected behavior depending on what the main thread is doing simultaneously. Any
  exceptions from the race condition would be caught by the try/except in the worker thread, so that's not causing the freeze either. But looking at the actual class attributes, I see all these are defined at the class level as mutable
  defaults—lists, dicts, sets—which means they're shared across all instances and could definitely be a source of issues if multiple instances exist or if there's concurrent access happening. I see the real problem
  now—`preview_block_cache` is defined as a class-level OrderedDict, and since nothing ever assigns `self.preview_block_cache = ...`, all instances are modifying the same shared dictionary in-place rather than having their own instance
  copies.

  Let me trace through the actual thread interactions more carefully. The `preview_generation` counter is only touched from the main thread, and `preview_pending` is also main-thread only, so those aren't the issue. I need to focus on
  what's actually being accessed across thread boundaries.

  The `preview_block_cache` is the only shared mutable state that multiple worker threads could hit simultaneously, but even that should be relatively safe under the GIL—worst case is a missed cache hit or a caught exception. So I'm
  still not seeing where the freeze would come from.

  Wait, let me trace through `_request_preview_render` more carefully. It increments `preview_generation` every single time it's called, not just when a preview actually renders. This gets triggered whenever a row is highlighted, the
  auto-preview toggle changes, or the render button is clicked, among other places.

  During a table refresh after scan completion, `_refresh_matches_table()` rebuilds the entire table and calls `_request_preview_render` at the end, but each row highlight event during that rebuild also triggers it independently. So
  `preview_generation` gets incremented many times in quick succession.

  When the user clicks "Render preview", it calls `_request_preview_render` with `force=True`, which increments the generation counter to some value N, sets `preview_pending = (row, N)`, and starts a timer. When that timer fires, it
  launches the worker with that specific generation number. But something happens between the timer being set and it actually firing that causes the preview to be stale.

  Between the timer being set and firing 0.18 seconds later, deferred row highlight events from the table rebuild might still be processing. If one fires and auto-preview is off, it increments `preview_generation` again, clears
  `preview_pending`, and stops the timer entirely. So the timer from the user's click never actually executes—the pending preview gets cancelled before it can start.

  But then the preview text would show "Auto-preview is off..." instead of staying frozen on "Rendering previews…", which is a different symptom. Unless the timing is even tighter: the timer fires and starts the worker with generation
  N, but before the worker completes, a row highlight event fires and increments `preview_generation` to N+1 while setting the text to "Auto-preview is off...". When the worker finishes, it sees the generation mismatch and tries to
  start the pending preview, but there's nothing pending anymore.

  Now let me trace through what happens when auto-preview is actually ON. The user enables it, which triggers a preview render request with the text set to "Rendering previews…" and a timer started. The timer fires and the worker begins
   processing. But while the worker is running, the user presses an arrow key to navigate rows.

  This triggers another preview request for the new row. The generation counter increments, a new pending state is set with the same "Rendering previews…" text, and a fresh timer starts. When the old worker finishes, it checks if its
  generation is still current—it's not, so it kicks off the pending preview. The new timer also fires and tries to start the pending preview, but the worker is already running so it returns immediately. Once the new worker completes,
  the cycle continues with any queued pending requests.

  The real concern here is that if a worker takes an extremely long time—say 30+ seconds for RAW file processing—the user sees "Rendering previews…" stuck on screen for that entire duration, which could feel like the interface is
  frozen. That's more of a performance issue than a bug though.

  Actually, let me reconsider this from a different angle. What if the `call_from_thread` callback never fires because the main thread is blocked?

  Looking at the code, nothing should block the main thread since all heavy work happens in workers. And `call_from_thread` is a core Textual mechanism that workers rely on, so it's unlikely to fail silently or be ignored by the event
  loop.

  Let me focus on a specific interaction: when `_refresh_matches_table` is called, it clears and rebuilds the table with new rows.

  The cursor lands on the first row, which triggers `RowHighlighted` and calls `_request_preview_render`. If auto-preview is on, this shows "Rendering previews…" and starts a timer. Adding subsequent rows probably doesn't trigger new
  `RowHighlighted` events since the cursor stays put, so that initial timer keeps running.

  Then at the end of `_refresh_matches_table`, it calls `_request_preview_render(focus)` again. Whether `focus` matches the first row or not, `_schedule_preview_start` will stop the old timer and start a fresh one. After the function
  returns, the timer should eventually fire and trigger the preview render.

  But I'm going in circles here. Let me step back and think about what the user is actually experiencing. They say the feature doesn't work, which usually means either the background task never finishes, the result never gets posted
  back to the UI, or there's a race condition blocking the work from starting. Looking at the recent changes, the preview feature introduces new config options for raw and image file extensions. I should focus on what's actually broken
  rather than tracing through the entire flow again. Checking the generation tracking through the preview pipeline—the generation counter increments first in `_request_preview_render`, then gets captured and passed through to the
  worker, ensuring we don't process stale preview requests.

  Now I'm tracing through a potential race condition: after setting `preview_pending` and scheduling the timer, `preview_generation` is at some value N, the timer fires and starts a worker with that generation, but while the worker
  runs, `_request_preview_render` gets called again and increments `preview_generation` to N+1. When the worker finishes with the stale generation N, the check correctly discards it and calls `_start_pending_preview()`, which finds
  nothing pending, so the old rendered text stays in the preview panel showing "Auto-preview is off...". This is actually the correct behavior—the stale result is properly discarded.

  Now I'm wondering if the issue might be in how the `run_worker` lambda is being called.

  Actually, thinking about this from Textual's perspective—when `run_worker` with `thread=True` creates a threaded worker, the worker runs the lambda and when it returns, Textual marks the worker as done. But the actual text delivery
  happens through `call_from_thread`, not through Textual's worker result mechanism. What if Textual cleans up something after the worker function returns that prevents `call_from_thread` from working properly?

  Wait, `call_from_thread` just adds a callback to the app's message queue, so it should work independently of the worker lifecycle. Unless the worker gets cancelled—but with `exclusive=False`, that shouldn't happen. Though I should
  verify that `group="preview"` with `exclusive=False` actually prevents worker cancellation.

  Actually, there might be an issue with `call_from_thread` when the worker thread exits. It could depend on thread-local state or require the thread to still be active.

  Let me think through the most likely scenario: `_request_preview_render` gets called constantly (on every row highlight, every table refresh), incrementing `preview_generation` each time. By the time the worker finishes, its
  generation number no longer matches the current `self.preview_generation`, so the rendered text gets discarded. And no new worker starts because `preview_pending` was already set to None by an intervening call with auto-preview
  disabled.

  But that doesn't match what I'm seeing—the message says "Rendering previews…", not "Auto-preview is off...". So either the user is still triggering auto-preview, or something else is going on.

  Actually, if auto-preview is ON, then each `_request_preview_render` call displays "Rendering previews…" and resets the timer. When the timer finally fires and the worker completes, the generation might not match anymore due to events
   that happened while the worker was running. The text gets discarded, but then `_start_pending_preview` checks if there's a pending request—if the last event set one, a new worker starts; otherwise nothing happens. So if the final
  event during the worker's execution was a row highlight with auto-preview still ON, then `preview_generation` got incremented, `preview_pending` was set with the new generation, and a timer was scheduled. When the old worker's
  callback runs, it marks `preview_inflight` as False, sees the generation mismatch, and calls `_start_pending_preview`, which finds `preview_pending` is set and starts a new worker.