param(
    [int]$GamePid,
    [string]$Keys = "",      # comma-separated VK codes (hex like 0x0D or names: enter,space,w,a,s,d,up,down,left,right,lshift)
    [int]$Hold = 150,        # ms to hold each key
    [int]$Between = 1500,    # ms between keys
    [int]$SettleMs = 2500,   # ms to wait before screenshot
    [string]$Shot = ""       # path to save screenshot (empty = no shot)
)
Add-Type -AssemblyName System.Drawing
Add-Type @'
using System;
using System.Runtime.InteropServices;
using System.Threading;
public class Drv {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, IntPtr pid);
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool attach);
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
  public static bool Focus(IntPtr h) {
    for (int i = 0; i < 10; i++) {
      if (GetForegroundWindow() == h) return true;
      // ALT tap unlocks SetForegroundWindow for this process
      keybd_event(0x12, 0, 0, UIntPtr.Zero);
      keybd_event(0x12, 0, 2, UIntPtr.Zero);
      uint fgThread = GetWindowThreadProcessId(GetForegroundWindow(), IntPtr.Zero);
      uint me = GetCurrentThreadId();
      AttachThreadInput(me, fgThread, true);
      ShowWindow(h, 9); // SW_RESTORE
      BringWindowToTop(h);
      SetForegroundWindow(h);
      AttachThreadInput(me, fgThread, false);
      Thread.Sleep(250);
    }
    return GetForegroundWindow() == h;
  }
  [DllImport("user32.dll")] public static extern uint MapVirtualKey(uint code, uint mapType);
  public static void Tap(byte vk, int holdMs) {
    byte scan = (byte)MapVirtualKey(vk, 0); // MAPVK_VK_TO_VSC
    keybd_event(vk, scan, 0x08, UIntPtr.Zero);        // KEYEVENTF_SCANCODE-style down (vk kept for compat)
    keybd_event(vk, scan, 0x00, UIntPtr.Zero);        // plain down too
    Thread.Sleep(holdMs);
    keybd_event(vk, scan, 0x02, UIntPtr.Zero);        // up
    keybd_event(vk, scan, 0x08 | 0x02, UIntPtr.Zero); // scancode up
  }
}
'@
$vkmap = @{ 'enter'=0x0D; 'space'=0x20; 'lshift'=0xA0; 'esc'=0x1B;
            'w'=0x57; 'a'=0x41; 's'=0x53; 'd'=0x44; 'q'=0x51; 'e'=0x45; 'r'=0x52;
            'i'=0x49; 'j'=0x4A; 'k'=0x4B; 'l'=0x4C;
            'up'=0x26; 'down'=0x28; 'left'=0x25; 'right'=0x27 }
$p = Get-Process -Id $GamePid -ErrorAction Stop
$h = $p.MainWindowHandle
if (-not [Drv]::Focus($h)) { Write-Output "FOCUS FAILED - no keys sent"; exit 1 }
if ($Keys -ne "") {
    foreach ($k in $Keys.Split(',')) {
        $k = $k.Trim().ToLower()
        if ($vkmap.ContainsKey($k)) { $vk = [byte]$vkmap[$k] } else { $vk = [byte]([Convert]::ToInt32($k, 16)) }
        if (-not [Drv]::Focus($h)) { Write-Output "FOCUS LOST at key $k - stopping"; exit 1 }
        [Drv]::Tap($vk, $Hold)
        Start-Sleep -Milliseconds $Between
    }
}
if ($Shot -ne "") {
    Start-Sleep -Milliseconds $SettleMs
    $r = New-Object Drv+RECT
    [Drv]::GetWindowRect($h, [ref]$r) | Out-Null
    $bmp = New-Object System.Drawing.Bitmap(($r.R-$r.L), ($r.B-$r.T))
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $hdc = $g.GetHdc()
    [Drv]::PrintWindow($h, $hdc, 2) | Out-Null   # 2 = PW_RENDERFULLCONTENT (captures DX content)
    $g.ReleaseHdc($hdc)
    $bmp.Save($Shot)
    $g.Dispose(); $bmp.Dispose()
    Write-Output "shot saved: $Shot"
}
