Option Explicit
Dim shell, fso, root, playerPath, pythonw, wmi, procs, proc
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
playerPath = root & "\player.py"
If Not fso.FileExists(playerPath) Then MsgBox "player.py not found next to this file.", 16, "NOT soundLARP 100%" : WScript.Quit 1
shell.CurrentDirectory = root

' Close an old copy of this player first
On Error Resume Next
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set procs = wmi.ExecQuery("Select * from Win32_Process Where Name = 'pythonw.exe' Or Name = 'python.exe'")
For Each proc In procs
  If Not IsNull(proc.CommandLine) Then
    If InStr(1, proc.CommandLine, playerPath, 1) > 0 Then proc.Terminate
  End If
Next
WScript.Sleep 700

pythonw = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Python\pythoncore-3.14-64\pythonw.exe"
If Not fso.FileExists(pythonw) Then pythonw = "pyw"
shell.Run """" & pythonw & """ """ & playerPath & """", 0, False
If Err.Number <> 0 Then MsgBox "Could not start Python. Run start_player_debug.bat to see the error.", 16, "NOT soundLARP 100%"
