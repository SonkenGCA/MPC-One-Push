"""Guarded arrangement operations; accepts an actual or simulated Live Song."""
import math

def clip_length(c):
    """Arrangement extent is independent of the source loop length."""
    return float(c.end_time-c.start_time) if hasattr(c, "end_time") else float(c.length)

def capture_group(song, selection):
    """Keep Live object identities as well as metadata so a reordered/deleted source is rejected."""
    if not isinstance(selection, list) or not 1 <= len(selection) <= 128:
        raise ValueError('Select between 1 and 128 clips')
    entries, seen = [], set()
    for item in selection:
        t, start, length, name = item
        t, start, length = int(t), float(start), float(length)
        key = (t, start, length, str(name))
        if key in seen:
            raise ValueError('Duplicate clip in selection')
        seen.add(key)
        tr, c = find_clip(song, *key)
        entries.append((t, tr, c, (c.start_time, clip_length(c), c.name)))
    return dict(clips=entries, base_track=min(e[0] for e in entries),
                start=min(e[3][0] for e in entries),
                end=max(e[3][0]+e[3][1] for e in entries))

def paste_group(song, group, target_track, at):
    """Preflight the entire group before touching Live; clean up only new copies on failure."""
    if not math.isfinite(at) or at < 0:
        raise ValueError('Invalid paste position')
    plan = []
    for t, tr, c, fingerprint in group['clips']:
        if t >= len(song.tracks) or song.tracks[t] != tr or c not in tr.arrangement_clips:
            raise ValueError('Source tracks/clips changed; copy again')
        if (c.start_time, clip_length(c), c.name) != fingerprint:
            raise ValueError('Source clip changed; copy again')
        dest_index = target_track+t-group['base_track']
        if not 0 <= dest_index < len(song.tracks):
            raise ValueError('Not enough destination tracks')
        dst = song.tracks[dest_index]
        if tr.is_frozen or bool(dst.has_midi_input) != bool(c.is_midi_clip):
            raise ValueError('Unfreeze sources and match MIDI/audio destination tracks')
        dest = at+c.start_time-group['start']
        empty_destination(song, dst, dest, clip_length(c))
        for other_dst, _, other_at, other_len in plan:
            if dst == other_dst and dest < other_at+other_len-1e-5 and dest+clip_length(c) > other_at+1e-5:
                raise ValueError('Selected clips would overlap')
        plan.append((dst, c, dest, clip_length(c)))
    created = []
    song.begin_undo_step()
    try:
        for dst, c, dest, _ in plan:
            new = dst.duplicate_clip_to_arrangement(c, dest)
            created.append((dst, new))
    except Exception:
        failed = False
        for dst, new in reversed(created):
            try:
                dst.delete_clip(new)
            except Exception:
                failed = True
        if failed:
            raise RuntimeError('Copy incomplete; use Undo to remove remaining copies')
        raise
    finally:
        song.end_undo_step()
    return len(created)

def find_clip(song, track, start, length, name):
    if not 0 <= track < len(song.tracks):
        raise ValueError('Track changed; select the clip again')
    tr = song.tracks[track]
    matches = [c for c in tr.arrangement_clips if abs(c.start_time-start) < 1e-5
               and abs(clip_length(c)-length) < 1e-5 and c.name == name]
    if len(matches) != 1:
        raise ValueError('Clip changed; select it again')
    return tr, matches[0]

def empty_destination(song, tr, at, length):
    if song.record_mode or tr.is_frozen:
        raise ValueError('Stop recording / unfreeze the track before editing')
    if not math.isfinite(at) or at < 0 or at+length > 1576800:
        raise ValueError('Destination outside the arrangement')
    if any(c.start_time < at+length-1e-5 and c.start_time+clip_length(c) > at+1e-5
           for c in tr.arrangement_clips):
        raise ValueError('Destination overlaps a clip; choose empty space')

def detail(c, left, right):
    """Bounded thumbnail data in clip time; audio positions are supplied by Live's warp map."""
    data = dict(start=float(c.start_time), length=float(clip_length(c)), name=c.name,
                color=int(c.color), muted=bool(c.muted), midi=bool(c.is_midi_clip),
                offset=float(c.start_marker), looping=bool(c.looping),
                loop_start=float(c.loop_start), loop_end=float(c.loop_end))
    if c.is_midi_clip:
        notes = c.get_notes_extended(0, 128, min(0., c.start_marker, c.loop_start),
                                    max(c.end_marker, c.loop_end, c.start_marker+clip_length(c))+1)
        data['notes'] = [(n.pitch, n.start_time, n.duration, n.velocity, n.mute) for n in notes[:512]]
        data['truncated'] = len(notes) > 512
    else:
        data['file'] = c.file_path
        data['frames'] = c.sample_length
        data['rate'] = c.sample_rate
        # A fixed-size map for the visible portion also handles tempo changes in warped clips.
        a, b = max(0., left-c.start_time), min(clip_length(c), right-c.start_time)
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
                seconds = c.loop_start+rel*(c.loop_end-c.loop_start)/max(clip_length(c), 1e-6)
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
               and abs(clip_length(c)-length) < 1e-6 and c.name == name]
    if len(matches) != 1:
        raise ValueError('Clip changed; select it again')
    source = matches[0]
    if operation not in ('delete', 'duplicate', 'move', 'mute'):
        raise ValueError('Unsupported arrangement operation')
    if operation in ('duplicate', 'move'):
        if not math.isfinite(destination) or destination < 0 or destination+length > 1576800:
            raise ValueError('Destination outside the arrangement')
        if any(c.start_time < destination+length-1e-6 and c.start_time+clip_length(c) > destination+1e-6 for c in clips):
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
