"""Guarded arrangement operations; accepts an actual or simulated Live Song."""
import math

def edit(song, track, start, length, name, operation, destination):
    if track < 0 or track >= len(song.tracks):
        raise ValueError('Track changed; select the clip again')
    tr = song.tracks[track]
    if song.record_mode or tr.is_frozen:
        raise ValueError('Stop recording / unfreeze the track before editing')
    clips = list(tr.arrangement_clips)
    matches = [c for c in clips if abs(c.start_time-start) < 1e-6
               and abs(c.length-length) < 1e-6 and c.name == name]
    if len(matches) != 1:
        raise ValueError('Clip changed; select it again')
    source = matches[0]
    if operation not in ('delete', 'duplicate', 'move'):
        raise ValueError('Unsupported arrangement operation')
    if operation != 'delete':
        if not math.isfinite(destination) or destination < 0 or destination+length > 1576800:
            raise ValueError('Destination outside the arrangement')
        if any(c.start_time < destination+length-1e-6 and c.start_time+c.length > destination+1e-6 for c in clips):
            raise ValueError('Destination overlaps a clip; choose empty space')
    song.begin_undo_step()
    try:
        if operation != 'delete':
            tr.duplicate_clip_to_arrangement(source, destination)
        if operation in ('delete', 'move'):
            tr.delete_clip(source)
    finally:
        song.end_undo_step()
