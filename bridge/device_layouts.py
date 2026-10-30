"""Named knob pages for Live's stock devices, like Push's device banks.

DEVICE_LAYOUTS maps a device's class name to [(page name, [entries])]. An entry is a parameter name, a
pattern with * (fnmatch, e.g. "OSC1*" or "* (Low)"), or "#n" for parameter number n (racks: macros by
position, since you can rename them). A page that matches more than 8 parameters is split into "Name",
"Name 2", ...; names a device doesn't have are skipped; a parameter is only placed once; and whatever is left
over becomes "More" pages, so no parameter is ever hidden.

Parameter names come from tools/device_params.json, read from Live 12.4 by tools/dump_device_params.py
(2026-10-29). Max for Live devices (Drum Sampler, LFO, Shaper, ...) can't be loaded by name from a script, so
they have no layout and show the plain knob pages.
"""

from fnmatch import fnmatchcase

PAGE = 8

_RACK = [("Macros", ["#%d" % i for i in range(1, 9)]), ("Macros 9-16", ["#%d" % i for i in range(9, 17)])]
_SC = [("Sidechain", ["S/C*"])]


def _eq8_band(n):
    return ("Band %d" % n, ["%d Filter On A" % n, "%d Filter Type A" % n, "%d Frequency A" % n, "%d Gain A" % n,
                            "%d Q A" % n])


DEVICE_LAYOUTS = {
    # ---------------------------------------------------------------- instruments
    "OriginalSimpler": [
        ("Sample", ["S Start", "S Length", "S Loop On", "S Loop Length", "S Loop Fade", "Transpose", "Detune",
                    "Volume"]),
        ("Amp Env", ["Ve Attack", "Ve Decay", "Ve Sustain", "Ve Release", "Vol < Vel", "Pan", "Spread",
                     "Trigger Mode"]),
        ("Filter", ["F On", "Filter Type", "Filter Freq", "Filter Res", "Filter Drive", "Filter Slope",
                    "Filt < Vel", "Filt < Key"]),
        ("Filter Env", ["Fe On", "Fe < Env", "Fe Attack", "Fe Decay", "Fe Sustain", "Fe Release",
                        "Filter Circuit - LP/HP", "Filter Morph"]),
        ("LFO", ["L On", "L Wave", "L Sync", "L Rate", "L Sync Rate", "L Attack", "Vol < LFO", "Filt < LFO"]),
        ("Pitch", ["Pe On", "Pe < Env", "Pe Attack", "Pe Decay", "Pe Sustain", "Pe Release", "Glide Mode",
                   "Glide Time"]),
        ("Fades", ["Fade In", "Fade Out", "Pitch < LFO", "Pan < LFO", "Pan < Rnd", "Ve Mode", "Ve Loop",
                   "Ve Retrig"]),
    ],
    "MultiSampler": [
        ("Main", ["Transpose", "Detune", "Volume", "Pan", "Spread", "Vol < Vel", "Glide Mode", "Glide Time"]),
        ("Amp Env", ["Ve Attack", "Ve Decay", "Ve Sustain", "Ve Release", "Ve Init", "Ve Peak", "Ve Mode",
                     "Ve Loop"]),
        ("Filter", ["F On", "Filter Type", "Filter Freq", "Filter Res", "Filter Drive", "Filter Slope",
                    "Filter Morph", "Filt < Key"]),
        ("Filter Env", ["Fe On", "Fe < Env", "Fe Attack", "Fe Decay", "Fe Sustain", "Fe Release", "Fe Init",
                        "Fe Peak"]),
        ("Modulation", ["Pitch < LFO", "Vol < LFO", "Pan < LFO", "Filt < LFO", "Filt < Vel", "L 1 On", "L 2 On",
                        "L 3 On"]),
        ("Amp Env More", ["Ve*"]), ("Filter Env More", ["Fe*"]),
    ],
    "Drift": [
        ("Main", ["Osc 1 Shape", "Osc 2 Detune", "LP Freq", "LP Res", "Env 1 Attack", "Env 1 Decay",
                  "Env 1 Sustain", "Env 1 Release"]),
        ("Osc 1", ["Osc 1 On", "Osc 1 Wave", "Osc 1 Oct", "Osc 1 Shape", "Osc 1 Shape Mod Amt", "Osc 1 Gain",
                   "Osc 1 Flt On"]),
        ("Osc 2", ["Osc 2 On", "Osc 2 Wave", "Osc 2 Oct", "Osc 2 Detune", "Osc 2 Gain", "Osc 2 Flt On",
                   "Noise On", "Noise Gain"]),
        ("Filter", ["LP Freq", "LP Res", "LP Type", "HP Freq", "Key > LPF", "LP Mod Amt 1", "LP Mod Amt 2",
                    "Noise Flt On"]),
        ("Env 2", ["Env 2 Attack", "Env 2 Decay", "Env 2 Sustain", "Env 2 Release", "Env 2 Cyc On"]),
        ("Cycling Env", ["Cyc Env*"]),
        ("LFO", ["LFO*"]),
        ("Mod", ["Mod Matrix Amt 1", "Mod Matrix Amt 2", "Mod Matrix Amt 3", "Pitch Mod Amt 1", "Pitch Mod Amt 2",
                 "Vel > Vol"]),
        ("Global", ["Volume", "Transpose", "Glide Time", "Drift", "Spread", "Strength", "Thickness",
                    "Poly Voice Depth"]),
    ],
    "InstrumentVector": [
        ("Main", ["Osc 1 Pos", "Osc 2 Pos", "Flt 1 Freq", "Flt 1 Res", "Amp Attack", "Amp Decay", "Amp Sustain",
                  "Amp Release"]),
        ("Osc 1", ["Osc 1*"]),
        ("Osc 2", ["Osc 2*"]),
        ("Sub", ["Sub*"]),
        ("Filter 1", ["Flt 1 On", "Flt 1 Type", "Flt 1 Freq", "Flt 1 Res", "Flt 1 Drive", "Flt 1 Slope",
                      "Flt 1 Morph", "Flt 1 LP/HP"]),
        ("Filter 2", ["Flt 2 On", "Flt 2 Type", "Flt 2 Freq", "Flt 2 Res", "Flt 2 Drive", "Flt 2 Slope",
                      "Flt 2 Morph", "Flt 2 LP/HP"]),
        ("Amp Env", ["Amp*"]),
        ("Env 2", ["Env 2*"]),
        ("Env 3", ["Env 3*"]),
        ("LFO 1", ["LFO 1*"]),
        ("LFO 2", ["LFO 2*"]),
        ("Global", ["Volume", "Transpose", "Glide", "Unison Amount", "Global Mod Amount", "Time"]),
    ],
    "Operator": [
        ("Main", ["Algorithm", "Osc-A Level", "Osc-B Level", "Osc-C Level", "Osc-D Level", "Filter Freq",
                  "Filter Res", "Volume"]),
        ("Osc A", ["Osc-A On", "A Coarse", "A Fine", "Osc-A Wave", "Osc-A Feedb", "Osc-A Level", "Osc-A Phase",
                   "A Fix On "]),
        ("Env A", ["Ae Attack", "Ae Decay", "Ae Sustain", "Ae Release", "Ae Init", "Ae Peak", "Ae Mode",
                   "Ae Loop"]),
        ("Osc B", ["Osc-B On", "B Coarse", "B Fine", "Osc-B Wave", "Osc-B Feedb", "Osc-B Level", "Osc-B Phase",
                   "B Fix On "]),
        ("Env B", ["Be Attack", "Be Decay", "Be Sustain", "Be Release", "Be Init", "Be Peak", "Be Mode",
                   "Be Loop"]),
        ("Osc C", ["Osc-C On", "C Coarse", "C Fine", "Osc-C Wave", "Osc-C Feedb", "Osc-C Level", "Osc-C Phase",
                   "C Fix On "]),
        ("Env C", ["Ce Attack", "Ce Decay", "Ce Sustain", "Ce Release", "Ce Init", "Ce Peak", "Ce Mode",
                   "Ce Loop"]),
        ("Osc D", ["Osc-D On", "D Coarse", "D Fine", "Osc-D Wave", "Osc-D Feedb", "Osc-D Level", "Osc-D Phase",
                   "D Fix On "]),
        ("Env D", ["De Attack", "De Decay", "De Sustain", "De Release", "De Init", "De Peak", "De Mode",
                   "De Loop"]),
        ("Filter", ["Filter On", "Filter Type", "Filter Freq", "Filter Res", "Filter Drive", "Filter Slope",
                    "Filter Morph", "Fe Amount"]),
        ("Filter Env", ["Fe Attack", "Fe Decay", "Fe Sustain", "Fe Release", "Fe Init", "Fe Peak", "Fe End",
                        "Fe Mode"]),
        ("LFO", ["LFO On", "LFO Type", "LFO Range", "LFO Rate", "LFO Sync", "LFO Amt", "LFO Amt A", "LFO Amt B"]),
        ("Pitch Env", ["Pe On", "Pe Amount", "Pe Attack", "Pe Decay", "Pe Sustain", "Pe Release", "Pe Init",
                       "Pe Peak"]),
        ("Global", ["Volume", "Transpose", "Tone", "Spread", "Panorama", "Glide On", "Glide Time", "Time"]),
        ("Shaper", ["Shaper Type", "Shaper Drive", "Shaper Mix"]),
        ("Osc A More", ["Osc-A*", "A *"]), ("Osc B More", ["Osc-B*", "B *"]), ("Osc C More", ["Osc-C*", "C *"]),
        ("Osc D More", ["Osc-D*", "D *"]), ("Env More", ["Ae *", "Be *", "Ce *", "De *"]),
        ("LFO More", ["LFO*", "Le *"]), ("Pitch Env More", ["Pe *"]), ("Filter More", ["Filt*", "Fe *"]),
    ],
    "UltraAnalog": [
        ("Main", ["OSC1 Shape", "OSC2 Shape", "OSC2 Detune", "F1 Freq", "F1 Resonance", "AEG1 Attack",
                  "AEG1 Decay", "AEG1 Rel"]),
        ("Osc 1", ["OSC1*", "O1*", "PEG1*"]),
        ("Filter 1", ["F1*"]),
        ("Filter Env 1", ["FEG1*"]),
        ("Amp 1", ["AMP1*", "A1*"]),
        ("Amp Env 1", ["AEG1*"]),
        ("LFO 1", ["LFO1*"]),
        ("Osc 2", ["OSC2*", "O2*", "PEG2*"]),
        ("Filter 2", ["F2*"]),
        ("Filter Env 2", ["FEG2*"]),
        ("Amp 2", ["AMP2*", "A2*"]),
        ("Amp Env 2", ["AEG2*"]),
        ("LFO 2", ["LFO2*"]),
        ("Noise", ["Noise*"]),
        ("Global", ["Volume", "Octave", "Semitone", "Detune", "Voices", "Glide On/Off", "Glide Time",
                    "Unison On/Off"]),
        ("Vibrato", ["Vib*"]),
    ],
    "Collision": [
        ("Main", ["Structure", "Mallet Stiffness", "Res 1 Decay", "Res 1 Material", "Res 1 Brightness",
                  "Res 1 Tune", "Res 2 Decay", "Volume"]),
        ("Mallet", ["Mallet*"]),
        ("Noise", ["Noise*"]),
        ("Res 1", ["Res 1 On/Off", "Res 1 Type", "Res 1 Tune", "Res 1 Decay", "Res 1 Material", "Res 1 Radius",
                   "Res 1 Brightness", "Res 1 Volume"]),
        ("Res 1 More", ["Res 1*"]),
        ("Res 2", ["Res 2 On/Off", "Res 2 Type", "Res 2 Tune", "Res 2 Decay", "Res 2 Material", "Res 2 Radius",
                   "Res 2 Brightness", "Res 2 Volume"]),
        ("Res 2 More", ["Res 2*"]),
        ("LFO", ["LFO*"]),
    ],
    "StringStudio": [
        ("Main", ["Exciter Type", "Exc Stiffness", "Exc Velocity", "String Decay", "Str Damping", "Pickup Pos",
                  "Body Type", "Volume"]),
        ("Excitator", ["Exc*", "E Pos*"]),
        ("String", ["String*", "Str*", "S *", "Stretch", "Error"]),
        ("Damper", ["Damp*", "D *"]),
        ("Termination", ["Term*", "T *"]),
        ("Body", ["Body*", "Pickup*"]),
        ("Filter", ["Filter*", "Freq*", "Reso*"]),
        ("Filter Env", ["FEG*"]),
        ("LFO", ["LFO*"]),
        ("Vibrato", ["Vib*"]),
        ("Global", ["Volume", "Octave", "Semitone", "Fine Tune", "Voices", "Porta On/Off", "Porta Time",
                    "Unison On/Off"]),
    ],
    "LoungeLizard": [
        ("Main", ["M Stiffness", "M Force", "F Tine Decay", "F Tine Vol", "F Tone Decay", "P Symmetry",
                  "P Distance", "Volume"]),
        ("Mallet", ["M *", "Noise*"]),
        ("Fork", ["F *"]),
        ("Pickup & Damper", ["P *", "Pickup Model", "Damp*"]),
        ("Global", ["Volume", "Voices", "Semitone", "Detune", "KB Stretch", "PB Range"]),
    ],
    "InstrumentMeld": [
        ("Main", ["Engine*", "A Osc Shape", "A Osc Tone", "B Osc Shape", "B Osc Tone", "A Filter Freq",
                  "B Filter Freq", "Volume"]),
        ("A Osc", ["A On", "A Osc Type", "A Osc Shape", "A Osc Tone", "A Transpose", "A Octave", "A Detune",
                   "A Volume"]),
        ("A Filter", ["A Filter*"]),
        ("A Amp Env", ["A Amp*"]),
        ("A Mod Env", ["A Mod*"]),
        ("A LFO 1", ["A LFO 1*"]),
        ("A LFO 2", ["A LFO 2*"]),
        ("B Osc", ["B On", "B Osc Type", "B Osc Shape", "B Osc Tone", "B Transpose", "B Octave", "B Detune",
                   "B Volume"]),
        ("B Filter", ["B Filter*"]),
        ("B Amp Env", ["B Amp*"]),
        ("B Mod Env", ["B Mod*"]),
        ("B LFO 1", ["B LFO 1*"]),
        ("B LFO 2", ["B LFO 2*"]),
        ("Global", ["Volume", "Drive*", "Limiter*", "Link*", "Scale*", "Voice*", "Mono*", "Engine*"]),
    ],
    "InstrumentImpulse": [("Pad %d" % n, ["%d Start" % n, "%d Transpose" % n, "%d Stretch Factor" % n,
                                          "%d Filter Freq" % n, "%d Filter Res" % n, "%d Envelope Decay" % n,
                                          "%d Pan" % n, "%d Volume" % n]) for n in range(1, 9)]
    + [("Global", ["Global*"])] + [("Pad %d More" % n, ["%d *" % n]) for n in range(1, 9)],
    "ProxyInstrumentDevice": [("Main", ["*"])],
    "InstrumentGroupDevice": _RACK,
    "DrumGroupDevice": _RACK,
    # ---------------------------------------------------------------- MIDI effects
    "MidiEffectGroupDevice": _RACK,
    "MidiArpeggiator": [
        ("Main", ["Style", "Synced Rate", "Free Rate", "Sync On", "Gate", "Groove", "Offset", "Hold On"]),
        ("Retrigger", ["Repeats", "Retrigger Mode", "Ret. Interval"]),
        ("Transpose", ["Tranpose Mode", "Tranpose Key", "Transp. Steps", "Transp. Dist.", "Use Current Scale"]),
        ("Velocity", ["Velocity On", "Vel. Retrigger", "Velocity Decay", "Velocity Target"]),
    ],
    "MidiChord": [("Notes 1-4", ["Shift1", "Shift2", "Shift3", "Shift4", "Velocity1", "Velocity2", "Velocity3",
                                 "Velocity4"]),
                  ("Notes 5-6", ["Shift5", "Shift6", "Velocity5", "Velocity6", "Strum", "Strum Tension",
                                 "Strum Crescendo", "Use Current Scale"]),
                  ("Chance", ["Chance*"])],
    "MidiScale": [("Main", ["Base", "InternalScale", "Use Current Scale", "Transpose", "Fold", "Lowest", "Range"]),
                  ("Map", ["Map *"])],
    "MidiCcControl": [("Main", ["Mod Wheel", "Pitch Bend", "Pressure", "Custom A", "Custom B", "Custom C",
                                "Custom D", "Custom E"]), ("Custom", ["Custom*"])],
    # ---------------------------------------------------------------- audio effects
    "AudioEffectGroupDevice": _RACK,
    "Eq8": [("Gains", ["%d Gain A" % n for n in range(1, 9)]), ("Freqs", ["%d Frequency A" % n for n in range(1, 9)]),
            ("Q", ["%d Q A" % n for n in range(1, 9)])] + [_eq8_band(n) for n in range(1, 9)]
    + [("Global", ["Output", "Scale", "Adaptive Q"])],
    "FilterEQ3": [("Main", ["GainLo", "GainMid", "GainHi", "FreqLo", "FreqHi", "LowOn", "MidOn", "HighOn"])],
    "ChannelEq": [("Main", ["*"])],
    "Compressor2": [("Main", ["Threshold", "Ratio", "Attack", "Release", "Knee", "Makeup", "Output", "Dry/Wet"]),
                    ("Settings", ["Model", "Env Mode", "Auto Release On/Off", "LookAhead", "Expansion Ratio"])] + _SC,
    "GlueCompressor": [("Main", ["Threshold", "Ratio", "Attack", "Release", "Range", "Output", "Dry/Wet", "Peak Clip In"]),
                       ] + _SC,
    "MultibandDynamics": [("Main", ["Amount", "Time Scaling", "Output", "Low-Mid Crossover", "Mid-High Crossover",
                                    "Soft Knee On/Off", "Peak/RMS Mode"]),
                          ("Low", ["* (Low)"]), ("Mid", ["* (Mid)"]), ("High", ["* (High)"])] + _SC,
    "Gate": [("Main", ["Threshold", "Attack", "Hold", "Release", "Return", "Floor", "FlipMode", "LookAhead"])] + _SC,
    "Limiter": [("Main", ["Input Gain", "Ceiling", "Release", "Auto", "Lookahead", "Mode", "Threshold", "Output"]),
                ("Settings", ["Link", "M/S Link", "Routing", "Maximize On"])],
    "AutoFilter2": [("Main", ["Frequency", "Resonance", "Filter Type", "Filter Slope", "Drive", "Filter Morph",
                              "Env Amount", "Dry/Wet"]),
                    ("LFO", ["LFO Amount", "LFO Wave", "LFO Rate", "LFO 16th", "LFO Freq", "LFO Phase", "LFO Offset",
                             "LFO Spin"]),
                    ("Envelope", ["Env*"]), ("Output", ["Output", "Soft Clip On", "Circuit", "Pitch", "Formant",
                                                       "Control", "Morph Slope"])] + _SC,
    "AutoPan2": [("Main", ["Mode", "Amount", "Waveform", "Rate", "16th", "Frequency", "Phase", "Offset"])],
    "AutoShift": [("Main", ["Pitch St.", "Pitch Fine", "Formant Shift", "Strength", "Root", "Scale", "Glide Time",
                            "Dry/Wet"]),
                  ("Quantizer", ["Quantizer On", "Scale Aware", "Smooth On", "Smooth Time", "Attack Time",
                                 "Release Time", "Latch", "Formant Follow"]),
                  ("LFO", ["LFO*"]), ("Vibrato", ["Vibrato*"]), ("MIDI", ["MIDI*"])],
    "BeatRepeat": [("Main", ["Repeat", "Chance", "Interval", "Grid", "Gate", "Decay", "Variation", "Volume"]),
                   ("More", ["Offset", "Block Triplets", "Variation Type", "Pitch", "Pitch Decay", "Mix Type",
                             "Filter On", "Filter Freq"])],
    "Chorus2": [("Main", ["Mode", "Rate", "Amount", "Feedback", "Delay Time", "Width", "Warmth", "Dry/Wet"])],
    "Corpus": [("Main", ["Resonance Type", "Tune", "Decay", "Material", "Brightness", "Radius", "Ratio",
                         "Dry Wet"]),
               ("Resonator", ["Resonator Quality", "Transpose", "Fine", "Spread", "Inharmonics", "Opening", "Hit",
                              "Bleed"]),
               ("LFO", ["LFO*", "Spin", "Phase", "Offset"])],
    "Delay": [("Main", ["L Time", "R Time", "L 16th", "R 16th", "Feedback", "Filter Freq", "Filter Width",
                        "Dry/Wet"]),
              ("Sync", ["L Sync", "R Sync", "Link", "Ping Pong", "L Offset", "R Offset", "Freeze", "Filter On"]),
              ("LFO", ["LFO*"])],
    "Echo": [("Main", ["L Time", "L 16th", "R Time", "R 16th", "Feedback", "Reverb Level", "Stereo Width",
                       "Dry Wet"]),
             ("Time", ["L Sync", "L Synced", "L Sync Mode", "L Offset", "R Sync", "R Synced", "Link", "Channel Mode"]),
             ("Filter", ["Filter On", "HP Freq", "HP Res", "LP Freq", "LP Res"]),
             ("Mod", ["Mod*", "Env Mix", "Dly < Mod", "Flt < Mod"]),
             ("Character", ["Gate*", "Duck*", "Noise*", "Wobble*"]),
             ("Reverb", ["Reverb*", "Repitch*", "Input Gain", "Output"])],
    "DrumBuss": [("Main", ["Drive", "Drive Type", "Crunch", "Damping Freq", "Transients", "Boom Amt", "Boom Freq",
                           "Dry/Wet"])],
    "Tube": [("Main", ["Drive", "Tube Type", "Bias", "Tone", "Envelope", "Attack", "Release", "Dry/Wet"])],
    "Erosion2": [("Main", ["*"])],
    "Overdrive": [("Main", ["*"])],
    "Pedal": [("Main", ["Type", "Drive", "Bass", "Mid", "Treble", "Mid Freq", "Sub", "Dry/Wet"])],
    "Amp": [("Main", ["Amp Type", "Input Gain", "Bass", "Middle", "Treble", "Presence", "Volume", "Dry/Wet"])],
    "Cabinet": [("Main", ["*"])],
    "Saturator": [("Main", ["Drive", "Type", "Output", "Dry/Wet", "Color On", "Color Amt Low", "Color Amt Hi",
                            "Color Freq"]),
                  ("Waveshaper", ["WS*", "Threshold", "Post Clip Mode"])],
    "Roar": [("Main", ["Drive", "Tone Amt", "Tone Freq", "Shaper 1 Type", "Shaper 1 Amt", "Flt 1 Freq", "Feedback",
                       "Dry/Wet"]),
             ("Stage 1", ["Stage 1*", "Shaper 1*", "Flt 1*"]), ("Stage 2", ["Stage 2*", "Shaper 2*", "Flt 2*"]),
             ("Stage 3", ["Stage 3*", "Shaper 3*", "Flt 3*"]), ("Feedback", ["Feedback", "FB*", "Fb*"]),
             ("LFO 1", ["LFO 1*"]), ("LFO 2", ["LFO 2*"]), ("Envelope", ["Env*"]), ("Noise", ["Noise*"])] + _SC,
    "Redux2": [("Main", ["*"])],
    "Vinyl": [("Main", ["*"])],
    "Reverb": [("Main", ["Decay Time", "Room Size", "Predelay", "Diffusion", "Stereo Image", "Reflect Level",
                         "Diffuse Level", "Dry/Wet"]),
               ("Input", ["In Lo Cut On", "In Hi Cut On", "Input Freq", "Input Width", "ER Shape", "ER Spin*"]),
               ("Diffusion", ["Diff.*", "HiShelf Gain", "LowShelf Gain", "Density", "Scale"]),
               ("Chorus", ["Chorus*", "Freeze On", "Flat On", "Cut On", "Size Smoothing"])],
    "Hybrid": [("Main", ["Decay", "Size", "Predelay", "Damping", "Diffusion", "Blend", "Width", "Dry/Wet"]),
               ("Algorithm", ["Algo*", "Modulation", "Freeze*", "Vintage", "Bass Mono", "Routing"]),
               ("Shape", ["DH*", "Sh *", "Ti *", "Qz*", "Pr *"]), ("EQ", ["EQ*"])],
    "PhaserNew": [("Main", ["Mode", "Amount", "Mod Freq", "Mod Rate", "Feedback", "Center Freq", "Spread",
                            "Dry/Wet"]),
                  ("Mod", ["Mod*", "Spin*", "Duty Cycle", "Lfo Blend"]), ("Envelope", ["Env*"])],
    "Resonator": [("Main", ["Frequency", "Decay", "Color", "Width", "Mode", "Filter On", "Output", "Dry/Wet"]),
                  ("Notes", ["I Note", "II Pitch", "III Pitch", "IV Pitch", "V Pitch", "I Gain", "II Gain",
                             "III Gain"]),
                  ("Voices", ["I *", "II *", "III *", "IV *", "V *"])],
    "Shifter": [("Main", ["Mode", "Pitch Coarse", "Pitch Fine", "FShift Coarse", "RM Coarse", "Tone", "Wide",
                          "Dry/Wet"]),
                ("LFO", ["Lfo*"]), ("Envelope", ["Env*"]), ("Delay", ["Delay*"])],
    "Transmute": [("Main", ["Freq. Hz", "Note", "Decay", "High Damp", "Low Damp", "Stretch", "Shift", "Dry Wet"])],
    "Spectral": [("Freeze", ["On", "Frozen", "Unit", "Sync Interval", "S.Rate ms", "Mode", "Fade In", "Fade Out"]),
                 ("Delay", ["Delay*"])],
    "Vocoder": [("Main", ["Formant Shift", "Filter Width", "Lower Filter Band", "Upper Filter Band",
                          "Envelope Depth", "Enhance", "Output", "Dry/Wet"])],
    "StereoGain": [("Main", ["Stereo Width", "Mono", "Bass Mono", "Bass Freq", "Balance", "Output", "Left Inv",
                             "Right Inv"])],
    "FilterDelay": [("Delay 1", ["1 *"]), ("Delay 2", ["2 *"]), ("Delay 3", ["3 *"])],
    "GrainDelay": [("Main", ["Frequency", "Pitch", "Spray", "Random", "Feedback", "Beat Delay", "Time Delay",
                             "DryWet"])],
    "Looper": [("Main", ["*"])],
    "MidiNoteLength": [("Main", ["*"])],
    "MidiPitcher": [("Main", ["*"])],
    "MidiRandom": [("Main", ["*"])],
    "MidiVelocity": [("Main", ["*"])],
    "ProxyAudioEffectDevice": [("Main", ["*"])],
}


def _match(entry, names, used):
    if entry.startswith("#") and entry[1:].isdigit():
        i = int(entry[1:])
        return [i] if 0 < i < len(names) and i not in used else []
    if "*" not in entry:
        return [i for i, n in enumerate(names) if i > 0 and n == entry and i not in used][:1]
    return [i for i, n in enumerate(names) if i > 0 and i not in used and fnmatchcase(n, entry)]


def resolve(class_name, pnames):
    """[(page name, [parameter indices])] for a device, or None if it has no layout.
    Parameter 0 ("Device On") is the ON button, so it never goes on a page."""
    layout = DEVICE_LAYOUTS.get(class_name)
    if not layout:
        return None
    pages, used, shown = [], set(), set()
    for name, entries in layout:
        ps = []
        for e in entries:
            for i in _match(e, pnames, used | set(ps)):
                ps.append(i)
        shown.update(ps)
        if not (name == "Main" and len(layout) > 1):    # Main is a summary: its knobs stay on their own pages too
            used.update(ps)
        for k in range(0, len(ps), PAGE):
            pages.append((name if k == 0 else "%s (%d)" % (name, k // PAGE + 1), ps[k:k + PAGE]))
    rest = [i for i in range(1, len(pnames)) if i not in shown]
    for k in range(0, len(rest), PAGE):
        pages.append(("More" if len(rest) <= PAGE else "More %d" % (k // PAGE + 1), rest[k:k + PAGE]))
    return pages
