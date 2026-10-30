"""Resolve a clip and parameter with stale-selection guards before envelope edits."""
import math

def resolve(song, target):
    ti=int(target['track'])
    if not 0 <= ti < len(song.tracks):raise ValueError('Track no longer exists')
    tr = song.tracks[ti]
    if tr.name != target['track_name']:
        raise ValueError('Track changed; select the parameter again')
    if 'arr' in target:
        raise ValueError('Live does not expose Arrangement automation envelopes. Record Q-Link movements with Automation Arm; drawing is available for Session clip envelopes.')
    else:
        slot=int(target['slot'])
        if not 0 <= slot < len(tr.clip_slots):raise ValueError('Clip slot no longer exists')
        clip = tr.clip_slots[slot].clip
        if clip is None or clip.name != target['clip_name']:
            raise ValueError('Clip changed; select the parameter again')
        a, b = clip.loop_start, clip.loop_end
    kind = target['kind']
    if kind == 'device':
        dev = tr
        for k, i in enumerate(int(v) for v in target['path'].split('/')):
            dev = dev.devices[i] if k % 2 == 0 else dev.chains[i]
        if int(target['param']) < 0:raise ValueError('Invalid parameter')
        if dev.name != target['device_name']:
            raise ValueError('Device changed; select the parameter again')
        param = dev.parameters[int(target['param'])]
    else:
        param = tr.mixer_device.sends[int(target['param'])] if kind == 'send' else getattr(tr.mixer_device,kind)
    if target.get('param_name') and param.name != target['param_name']:
        raise ValueError('Parameter changed; select it again')
    if not getattr(param,'is_enabled',True) or not getattr(param,'is_automatable',True):
        raise ValueError('Live does not allow automation of this parameter')
    return tr, clip, param, float(a), float(b)

def execute(song, target, op, values=()):
    tr, clip, prm, a, b = resolve(song,target)
    if op == 'show':
        song.view.detail_clip = clip
        clip.view.select_envelope_parameter(prm)
        clip.view.show_envelope()
        op = 'get'
    if not all(math.isfinite(v) for v in (a,b)) or not 0 < b-a <= 1024:
        raise ValueError('Select a clip no longer than 256 bars')
    if op != 'get':
        if song.record_mode or tr.is_frozen:
            raise ValueError('Stop recording / unfreeze the track before drawing')
        if op not in ('step','clear'):
            raise ValueError('Unsupported automation action')
        if op == 'step':
            at, length, value = map(float,values)
            if not all(math.isfinite(v) for v in (at,length,value)) or length <= 0 or not a <= at < b:
                raise ValueError('Automation point outside the clip')
        song.begin_undo_step()
        try:
            if op == 'clear':
                clip.clear_envelope(prm)
            else:
                env = clip.automation_envelope(prm)
                if env is None and 'arr' in target:
                    raise ValueError('Live requires this Arrangement envelope to exist first; record a parameter turn with Automation Arm, then draw here')
                env = env or clip.create_automation_envelope(prm)
                env.insert_step(at,min(length,b-at),max(prm.min,min(prm.max,value)))
        finally:
            song.end_undo_step()
    env = clip.automation_envelope(prm)
    return dict(a=a,b=b,label=prm.name,exists=env is not None,min=prm.min,max=prm.max,
                vals=[float(env.value_at_time(a+(b-a)*i/128)) if env else float(prm.value) for i in range(128)])
