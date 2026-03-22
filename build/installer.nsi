; Fibery Transcript NSIS Installer Script
; Installs per-user (no admin required) to %LOCALAPPDATA%\FiberyTranscript
; Supports clean upgrades: closes running instance, preserves settings,
; and writes installer_prefs.json for first-launch configuration merge.

!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "WordFunc.nsh"
!include "nsDialogs.nsh"
!include "LogicLib.nsh"
!include "WinMessages.nsh"

; --- Version (can be overridden via makensis -DVERSION=x.y.z) ---
!ifndef VERSION
    !define VERSION "1.3.0"
!endif

; --- General ---
Name "Fibery Transcript"
OutFile "..\dist\FiberyTranscript-${VERSION}-Setup.exe"
InstallDir "$LOCALAPPDATA\FiberyTranscript"
InstallDirRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\FiberyTranscript" "InstallLocation"
RequestExecutionLevel user
SetCompressor /SOLID lzma

; --- Version Info ---
VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "Fibery Transcript"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "FileDescription" "Fibery Transcript Installer"
VIAddVersionKey "FileVersion" "${VERSION}.0"
VIAddVersionKey "LegalCopyright" "Copyright Fibery Transcript"

; --- Interface ---
!define MUI_ICON "..\ui\static\icon.ico"
!define MUI_UNICON "..\ui\static\icon.ico"
!define MUI_ABORTWARNING

; --- Component descriptions ---
!define MUI_COMPONENTSPAGE_SMALLDESC

; --- Variables ---
Var SettingsDir          ; %APPDATA%\Fibery Transcript
Var SettingsFile         ; full path to settings.json
Var IsUpgrade            ; "1" if upgrading existing install

; Configuration page controls
Var ConfigPage
Var LblName
Var TxtName
Var LblStorage
Var RadioFibery
Var RadioLocal
Var LblRecDir
Var TxtRecDir
Var BtnBrowse

; Configuration values (pre-populated from existing settings or defaults)
Var CfgDisplayName
Var CfgAudioStorage      ; "fibery" or "local"
Var CfgRecordingsDir
Var CfgAutoStart          ; "true" or "false"

; --- Pages ---
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_COMPONENTS
Page custom ConfigPageCreate ConfigPageLeave
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; ============================================================
; Sections (defined before .onInit so ${SecStartup} is available)
; ============================================================
Section "Fibery Transcript" SecMain
    SectionIn RO  ; required, cannot be unchecked
    SetOutPath "$INSTDIR"

    ; Copy all files from PyInstaller output (overwrites existing on upgrade)
    File /r "..\dist\FiberyTranscript\*.*"

    ; Create uninstaller
    WriteUninstaller "$INSTDIR\Uninstall.exe"

    ; Start Menu shortcut
    CreateDirectory "$SMPROGRAMS\Fibery Transcript"
    CreateShortcut "$SMPROGRAMS\Fibery Transcript\Fibery Transcript.lnk" "$INSTDIR\FiberyTranscript.exe" "" "$INSTDIR\FiberyTranscript.exe"
    CreateShortcut "$SMPROGRAMS\Fibery Transcript\Uninstall.lnk" "$INSTDIR\Uninstall.exe"

    ; Desktop shortcut
    CreateShortcut "$DESKTOP\Fibery Transcript.lnk" "$INSTDIR\FiberyTranscript.exe" "" "$INSTDIR\FiberyTranscript.exe"

    ; Register uninstaller in Add/Remove Programs
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\FiberyTranscript" "DisplayName" "Fibery Transcript"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\FiberyTranscript" "UninstallString" '"$INSTDIR\Uninstall.exe"'
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\FiberyTranscript" "InstallLocation" "$INSTDIR"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\FiberyTranscript" "DisplayIcon" "$INSTDIR\FiberyTranscript.exe"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\FiberyTranscript" "Publisher" "Fibery Transcript Team"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\FiberyTranscript" "DisplayVersion" "${VERSION}"

    ; Calculate installed size
    ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
    IntFmt $0 "0x%08X" $0
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\FiberyTranscript" "EstimatedSize" "$0"

    ; --- Write installer_prefs.json for the app to merge on first launch ---
    CreateDirectory "$SettingsDir"

    ; Escape backslashes in recordings dir for JSON (\ -> \\)
    StrCpy $0 $CfgRecordingsDir
    ${If} $0 != ""
        ${WordReplace} $0 "\" "\\" "+" $1
    ${Else}
        StrCpy $1 ""
    ${EndIf}

    ; Determine auto_start value from section selection
    SectionGetFlags ${SecStartup} $2
    IntOp $2 $2 & 1
    ${If} $2 == 1
        StrCpy $3 "true"
    ${Else}
        StrCpy $3 "false"
    ${EndIf}

    FileOpen $4 "$SettingsDir\installer_prefs.json" w
    FileWrite $4 '{$\r$\n'
    FileWrite $4 '  "display_name": "$CfgDisplayName",$\r$\n'
    FileWrite $4 '  "audio_storage": "$CfgAudioStorage",$\r$\n'
    FileWrite $4 '  "recordings_dir": "$1",$\r$\n'
    FileWrite $4 '  "auto_start_on_boot": $3$\r$\n'
    FileWrite $4 '}$\r$\n'
    FileClose $4

SectionEnd

Section /o "Start on boot" SecStartup
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "FiberyTranscript" "$INSTDIR\FiberyTranscript.exe"
SectionEnd

; --- Section descriptions ---
!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
    !insertmacro MUI_DESCRIPTION_TEXT ${SecMain} "Installs Fibery Transcript and its shortcuts."
    !insertmacro MUI_DESCRIPTION_TEXT ${SecStartup} "Automatically start Fibery Transcript when Windows starts."
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; ============================================================
; Initialisation — detect upgrade, close running app, read settings
; (placed after sections so ${SecStartup} is defined)
; ============================================================
Function .onInit
    ; Determine settings directory
    StrCpy $SettingsDir "$APPDATA\Fibery Transcript"
    StrCpy $SettingsFile "$SettingsDir\settings.json"

    ; --- Check for existing installation (upgrade) ---
    ReadRegStr $0 HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\FiberyTranscript" "UninstallString"
    ${If} $0 != ""
        StrCpy $IsUpgrade "1"
    ${Else}
        StrCpy $IsUpgrade "0"
    ${EndIf}

    ; --- Close running instance gracefully ---
    FindWindow $0 "" "FiberyTranscript"
    ${If} $0 != 0
        MessageBox MB_OKCANCEL|MB_ICONINFORMATION \
            "Fibery Transcript is currently running and will be closed to continue the installation." \
            IDOK CloseApp
        Abort
        CloseApp:
        ; Send WM_CLOSE for graceful shutdown
        SendMessage $0 ${WM_CLOSE} 0 0
        Sleep 3000
        ; If still running, force kill
        FindWindow $0 "" "FiberyTranscript"
        ${If} $0 != 0
            nsExec::ExecToStack 'taskkill /IM FiberyTranscript.exe /F'
            Sleep 1000
        ${EndIf}
    ${EndIf}

    ; --- Set defaults ---
    StrCpy $CfgDisplayName ""
    StrCpy $CfgAudioStorage "fibery"
    StrCpy $CfgRecordingsDir ""
    StrCpy $CfgAutoStart "false"

    ; --- Read existing settings to pre-populate ---
    IfFileExists $SettingsFile 0 SkipReadSettings

    ; Use PowerShell to read JSON values (available on all modern Windows)
    ; Read display_name
    nsExec::ExecToStack 'powershell -NoProfile -NonInteractive -Command "try{$$s=Get-Content -Raw -LiteralPath ''$SettingsFile''|ConvertFrom-Json;if($$s.display_name){Write-Host $$s.display_name -NoNewline}}catch{}"'
    Pop $0  ; exit code
    Pop $1  ; output
    ${If} $1 != ""
        StrCpy $CfgDisplayName $1
    ${EndIf}

    ; Read audio_storage
    nsExec::ExecToStack 'powershell -NoProfile -NonInteractive -Command "try{$$s=Get-Content -Raw -LiteralPath ''$SettingsFile''|ConvertFrom-Json;if($$s.audio_storage){Write-Host $$s.audio_storage -NoNewline}}catch{}"'
    Pop $0
    Pop $1
    ${If} $1 != ""
        StrCpy $CfgAudioStorage $1
    ${EndIf}

    ; Read recordings_dir
    nsExec::ExecToStack 'powershell -NoProfile -NonInteractive -Command "try{$$s=Get-Content -Raw -LiteralPath ''$SettingsFile''|ConvertFrom-Json;if($$s.recordings_dir){Write-Host $$s.recordings_dir -NoNewline}}catch{}"'
    Pop $0
    Pop $1
    ${If} $1 != ""
        StrCpy $CfgRecordingsDir $1
    ${EndIf}

    ; Read auto_start_on_boot
    nsExec::ExecToStack 'powershell -NoProfile -NonInteractive -Command "try{$$s=Get-Content -Raw -LiteralPath ''$SettingsFile''|ConvertFrom-Json;if($$s.auto_start_on_boot -eq $$true){Write-Host ''true'' -NoNewline}else{Write-Host ''false'' -NoNewline}}catch{}"'
    Pop $0
    Pop $1
    ${If} $1 != ""
        StrCpy $CfgAutoStart $1
    ${EndIf}

    SkipReadSettings:

    ; Pre-check "Start on boot" section if already enabled
    ${If} $CfgAutoStart == "true"
        SectionGetFlags ${SecStartup} $0
        IntOp $0 $0 | 1
        SectionSetFlags ${SecStartup} $0
    ${EndIf}

FunctionEnd

; ============================================================
; Custom Configuration Page
; ============================================================
Function ConfigPageCreate
    !insertmacro MUI_HEADER_TEXT "Configuration" "Set your preferences (you can change these later in the app)."

    nsDialogs::Create 1018
    Pop $ConfigPage
    ${If} $ConfigPage == error
        Abort
    ${EndIf}

    ; --- Your first name ---
    ${NSD_CreateLabel} 0 0 100% 12u "Your first name (used to identify who is recording):"
    Pop $LblName
    ${NSD_CreateText} 0 16u 60% 14u "$CfgDisplayName"
    Pop $TxtName

    ; --- Save location ---
    ${NSD_CreateLabel} 0 46u 100% 12u "Default save location for transcripts and summaries:"
    Pop $LblStorage

    ${NSD_CreateRadioButton} 0 62u 30% 12u "Fibery"
    Pop $RadioFibery
    ${NSD_CreateRadioButton} 32% 62u 30% 12u "Local only"
    Pop $RadioLocal

    ; Pre-select the right radio button
    ${If} $CfgAudioStorage == "local"
        ${NSD_Check} $RadioLocal
    ${Else}
        ${NSD_Check} $RadioFibery
    ${EndIf}

    ; --- Recordings directory ---
    ${NSD_CreateLabel} 0 92u 100% 12u "Recordings folder (leave empty for default):"
    Pop $LblRecDir
    ${NSD_CreateText} 0 108u 78% 14u "$CfgRecordingsDir"
    Pop $TxtRecDir
    ${NSD_CreateButton} 80% 107u 20% 16u "Browse..."
    Pop $BtnBrowse
    ${NSD_OnClick} $BtnBrowse ConfigPageBrowse

    nsDialogs::Show
FunctionEnd

Function ConfigPageBrowse
    nsDialogs::SelectFolderDialog "Select recordings folder" "$DOCUMENTS"
    Pop $0
    ${If} $0 != error
        ${NSD_SetText} $TxtRecDir $0
    ${EndIf}
FunctionEnd

Function ConfigPageLeave
    ; Read values from controls
    ${NSD_GetText} $TxtName $CfgDisplayName
    ${NSD_GetText} $TxtRecDir $CfgRecordingsDir

    ; Determine which radio is checked
    ${NSD_GetState} $RadioLocal $0
    ${If} $0 == ${BST_CHECKED}
        StrCpy $CfgAudioStorage "local"
    ${Else}
        StrCpy $CfgAudioStorage "fibery"
    ${EndIf}
FunctionEnd

; ============================================================
; Uninstallation
; ============================================================
Section "Uninstall"
    ; Remove files
    RMDir /r "$INSTDIR"

    ; Remove shortcuts
    Delete "$SMPROGRAMS\Fibery Transcript\Fibery Transcript.lnk"
    Delete "$SMPROGRAMS\Fibery Transcript\Uninstall.lnk"
    RMDir "$SMPROGRAMS\Fibery Transcript"
    Delete "$DESKTOP\Fibery Transcript.lnk"

    ; Remove startup entry (if set)
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "FiberyTranscript"

    ; Remove registry entries
    DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\FiberyTranscript"
SectionEnd
