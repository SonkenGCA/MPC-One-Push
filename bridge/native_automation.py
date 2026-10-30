"""Native Arrangement automation fallback. Resolve controls by accessibility identity, never saved pixels.

The UI worker owns COM objects; the controller only receives image crops and immutable coordinates.
"""
import time


def unique(items, label):
    if len(items) != 1:
        raise ValueError('%s could not be identified uniquely in Live (%d matches)' % (label,len(items)))
    return items[0]


def choose_parameter(nodes, target):
    """nodes: (control, name, automation_id, ancestor names). Pure selection for regression checks."""
    nodes=[entry for entry in nodes if getattr(entry[0],'ControlTypeName','CustomControl') not in ('TextControl','GroupControl','PaneControl')]
    name = target['param_name']
    if target['kind']=='tempo':
        matches=[c for c,n,aid,parents in nodes if aid=='Transport.Tempo']
    elif target['kind'] == 'device':
        matches=[c for c,n,aid,parents in nodes if n==name and target['device_name'] in parents]
    else:
        scope=target.get('scope','track')
        prefix='ArrangementView.MainTrack.Mixer.' if scope=='master' else 'ArrangementView.Track[%d].Mixer.' % target['track']
        suffix={'volume':'Volume','panning':'Pan'}.get(target['kind'])
        matches=[c for c,n,aid,parents in nodes if aid==prefix+suffix] if suffix and scope!='return' else [
            c for c,n,aid,parents in nodes if aid.startswith(prefix) and n==name]
        if not matches:
            matches=[c for c,n,aid,parents in nodes if n==name and target['track_name'] in parents]
    return unique(matches,'Parameter '+name)


def crop_view(area, size, zoom, center):
    """Window-relative crop, clamped even after DPI/size changes."""
    x0,y0,x1,y1=area
    x0,y0=max(0,int(x0)),max(0,int(y0))
    x1,y1=min(size[0],int(x1)),min(size[1],int(y1))
    if x1-x0<32 or y1-y0<32:raise ValueError('Live Arrangement is not visible')
    width,height=(x1-x0)/zoom,(y1-y0)/zoom
    cx=x0+max(0.,min(1.,center[0]))*(x1-x0)
    cy=y0+max(0.,min(1.,center[1]))*(y1-y0)
    left=max(x0,min(x1-width,cx-width/2))
    top=max(y0,min(y1-height,cy-height/2))
    return tuple(map(int,(left,top,left+width,top+height)))


def point_to_window(x,y,place,scale,crop,frame_size):
    px,py=place
    if scale<=0 or not px<=x<px+frame_size[0] or not py<=y<py+frame_size[1]:
        raise ValueError('Touch outside the current Live image')
    return crop[0]+(x-px)/scale,crop[1]+(y-py)/scale


class NativeAutomation:
    def __init__(self,hwnd):
        import uiautomation as auto
        self.auto=auto
        self.root=auto.ControlFromHandle(hwnd)
        self.hwnd=hwnd

    def nodes(self):
        def walk(control,parents,depth):
            if depth>30:return
            for child in control.GetChildren():
                name,aid=child.Name,child.AutomationId
                yield child,name,aid,parents
                yield from walk(child,parents+(name,),depth+1)
        return list(walk(self.root,(),0))

    def open(self,target,is_current=lambda:True):
        import winshot
        if not winshot.focus(self.hwnd):raise ValueError('Bring Live to the foreground to open automation')
        def guard():
            if not is_current() or not winshot.is_foreground(self.hwnd):
                raise ValueError('Automation opening cancelled because the view or focused app changed')
        guard()
        control=choose_parameter(self.nodes(),target)
        if not control.IsEnabled:raise ValueError('This Live control is disabled')
        # SetFocus does not move the knob or write a parameter value.
        guard()
        if not control.SetFocus():raise ValueError('Live parameter could not receive focus')
        guard()
        self.auto.SendKeys('{Shift}{F10}',waitTime=0.05)
        menu=None
        for _ in range(12):
            nodes=self.nodes()
            choices=[c for c,n,aid,parents in nodes if n.split('\t')[0]=='Show Automation']
            if len(choices)==1:
                menu=choices[0];break
            time.sleep(.05)
        if menu is None:
            guard()
            self.auto.SendKeys('{Esc}',waitTime=0)
            raise ValueError('Live did not offer Show Automation for '+target['param_name'])
        pattern=menu.GetInvokePattern()
        if pattern is None:raise ValueError('Live automation menu cannot be invoked')
        guard()
        pattern.Invoke()
        time.sleep(.12)
        self.area()  # verify there is a real Arrangement editor before enabling touches

    def area(self):
        nodes=self.nodes()
        controls=[c for c,n,aid,parents in nodes if aid=='ArrangementView']
        control=unique(controls,'Arrangement editor')
        r=control.BoundingRectangle
        wr=self.root.BoundingRectangle
        return r.left-wr.left,r.top-wr.top,r.right-wr.left,r.bottom-wr.top
