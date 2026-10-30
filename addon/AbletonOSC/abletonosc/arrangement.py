"""Guarded arrangement operations; accepts an actual or simulated Live Song."""
import math

def find_clip(song, track, start, length, name):
    if not 0 <= track < len(song.tracks):
        raise ValueError('Track changed; select the clip again')
    tr = song.tracks[track]
    matches = [c for c in tr.arrangement_clips if abs(c.start_time-start) < 1e-5
               and abs(c.length-length) < 1e-5 and c.name == name]
    if len(matches) != 1:
        raise ValueError('Clip changed; select it again')
    return tr, matches[0]

def empty_destination(song, tr, at, length):
    if song.record_mode or tr.is_frozen:
        raise ValueError('Stop recording / unfreeze the track before editing')
    if not math.isfinite(at) or at < 0 or at+length > 1576800:
        raise ValueError('Destination outside the arrangement')
    if any(c.start_time < at+length-1e-5 and c.start_time+c.length > at+1e-5
           for c in tr.arrangement_clips):
        raise ValueError('Destination overlaps a clip; choose empty space')

def detail(c, left, right):
    """Bounded thumbnail data in clip time; audio positions are supplied by Live's warp map."""
    data = dict(start=float(c.start_time), length=float(c.length), name=c.name,
                color=int(c.color), muted=bool(c.muted), midi=bool(c.is_midi_clip),
                offset=float(c.start_marker), looping=bool(c.looping),
                loop_start=float(c.loop_start), loop_end=float(c.loop_end))
    if c.is_midi_clip:
        notes = c.get_notes_extended(0, 128, min(0., c.start_marker, c.loop_start),
                                    max(c.end_marker, c.loop_end, c.start_marker+c.length)+1)
        data['notes'] = [(n.pitch, n.start_time, n.duration, n.velocity, n.mute) for n in notes[:512]]
        data['truncated'] = len(notes) > 512
    else:
        data['file'] = c.file_path
        data['frames'] = c.sample_length
        data['rate'] = c.sample_rate
        # A fixed-size map for the visible portion also handles tempo changes in warped clips.
        a, b = max(0., left-c.start_time), min(c.length, right-c.start_time)
        data['range'] = [a, b]
        positions = []
        for i in range(257):
            rel = a+(b-a)*i/256
            if c.warping:
                beat = c.start_marker+rel
                if c.looping and c.loop_end > c.loop_start and beat >= c.loop_end:
                    beat = c.loop_start+(beat-c.loop_start) % (c.loop_end-c.loop_start)
                positions.append(c.beat_to_sample_time(beat))
            else:
                # Arrangement loop bounds delimit the visible unwarped region, in seconds.
                # end_marker may still describe the original Session source and is not the edge.
                seconds = c.loop_start+rel*(c.loop_end-c.loop_start)/max(c.length, 1e-6)
                positions.append(c.seconds_to_sample_time(seconds))
        # Both Live conversion functions return SAMPLE FRAMES, not seconds.
        data['sample_positions'] = positions
    return data

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
    if operation not in ('delete', 'duplicate', 'move', 'mute'):
        raise ValueError('Unsupported arrangement operation')
    if operation in ('duplicate', 'move'):
        if not math.isfinite(destination) or destination < 0 or destination+length > 1576800:
            raise ValueError('Destination outside the arrangement')
        if any(c.start_time < destination+length-1e-6 and c.start_time+c.length > destination+1e-6 for c in clips):
            raise ValueError('Destination overlaps a clip; choose empty space')
    song.begin_undo_step()
    try:
        if operation == 'mute':
            source.muted = not source.muted
        elif operation != 'delete':
            tr.duplicate_clip_to_arrangement(source, destination)
        if operation in ('delete', 'move'):
            tr.delete_clip(source)
    finally:
        song.end_undo_step()
