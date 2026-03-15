; Fibery Transcript NSIS Installer Script
; Installs per-user (no admin required) to %LOCALAPPDATA%\FiberyTranscript

!include "MUI2.nsh"
!include "FileFunc.nsh"

; --- General ---
Name "Fibery Transcript"
OutFile "..\dist\FiberyTranscript-Setup.exe"
InstallDir "$LOCALAPPDATA\FiberyTranscript"
RequestExecutionLevel user
SetCompressor /SOLID lzma

; --- Version Info ---
VIProductVersion "1.0.0.0"
VIAddVersionKey "ProductName" "Fibery Transcript"
VIAddVersionKey "ProductVersion" "1.0.0"
VIAddVersionKey "FileDescription" "Fibery Transcript Installer"
VIAddVersionKey "FileVersion" "1.0.0.0"
VIAddVersionKey "LegalCopyright" "Copyright Fibery Transcript"

; --- Interface ---
!define MUI_ICON "..\ui\static\icon.ico"
!define MUI_UNICON "..\ui\static\icon.ico"
!define MUI_ABORTWARNING

; --- Component descriptions ---
!define MUI_COMPONENTSPAGE_SMALLDESC

; --- Pages ---
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; --- Installation ---
Section "Fibery Transcript" SecMain
    SectionIn RO  ; required, cannot be unchecked
    SetOutPath "$INSTDIR"

    ; Copy all files from PyInstaller output
    File /r "..\dist\FiberyTranscript\*.*"

    ; Create uninstaller
    WriteUninstaller "$INSTDIR\Uninstall.exe"

    ; Start Menu shortcut
    CreateDirectory "$SMPROGRAMS\Fibery Transcript"
    CreateShortcut "$SMPROGRAMS\Fibery Transcript\Fibery Transcript.lnk" "$INSTDIR\FiberyTranscript.exe" "" "$INSTDIR\FiberyTranscript.exe"
    CreateShortcut "$SMPROGRAMS\Fibery Transcript\Uninstall.lnk" "$INSTDIR\Uninstall.exe"

    ; Desktop shortcut (optional)
    CreateShortcut "$DESKTOP\Fibery Transcript.lnk" "$INSTDIR\FiberyTranscript.exe" "" "$INSTDIR\FiberyTranscript.exe"

    ; Register uninstaller in Add/Remove Programs
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\FiberyTranscript" "DisplayName" "Fibery Transcript"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\FiberyTranscript" "UninstallString" '"$INSTDIR\Uninstall.exe"'
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\FiberyTranscript" "DisplayIcon" "$INSTDIR\FiberyTranscript.exe"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\FiberyTranscript" "Publisher" "Fibery Transcript Team"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\FiberyTranscript" "DisplayVersion" "1.0.0"

    ; Calculate installed size
    ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
    IntFmt $0 "0x%08X" $0
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\FiberyTranscript" "EstimatedSize" "$0"

SectionEnd

Section /o "Start on boot" SecStartup
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "FiberyTranscript" "$INSTDIR\FiberyTranscript.exe"
SectionEnd

; --- Section descriptions ---
!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
    !insertmacro MUI_DESCRIPTION_TEXT ${SecMain} "Installs Fibery Transcript and its shortcuts."
    !insertmacro MUI_DESCRIPTION_TEXT ${SecStartup} "Automatically start Fibery Transcript when Windows starts."
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; --- Uninstallation ---
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

