#Requires AutoHotkey v2.0
#SingleInstance Force
#NoTrayIcon
; Режим питания: окошко выбора «Экономия энергии» / «Высокая производительность».
; В «Экономии» выключен турбобуст CPU; таймеры сна и экрана такие же, как в «Высокой».

SAVER := "a1841308-3541-4fab-bc81-f71556f20b4a"  ; схема «Экономия энергии»
HIGH  := "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"  ; схема «Высокая производительность»
PROC  := "54533251-82be-4824-96c1-47b60b740d00"  ; подгруппа «Управление питанием процессора»
BOOST := "be337238-0d82-4146-a960-4f3749d470c7"  ; «Режим усиления производительности процессора»

; Запуск с параметром eco или high (так вызывает значок инвертора): переключить без окна.
; Код выхода: 0 - режим включён, 1 - не удалось, 2 - параметр непонятен.
if A_Args.Length {
    if A_Args[1] != "eco" && A_Args[1] != "high"
        ExitApp(2)
    SetMode(A_Args[1] = "eco", true)
}

isEco := GetActiveScheme() = SAVER

g := Gui("+AlwaysOnTop -MinimizeBox", "Режим питания")
g.SetFont("s11", "Segoe UI")
g.AddText("w320 Center", "Сейчас: " (isEco ? "экономия энергии" : "высокая производительность"))
bEco  := g.AddButton("w320 h48", "Экономия энергии")
bHigh := g.AddButton("w320 h48", "Высокая производительность")
bEco.OnEvent("Click", (*) => SetMode(true))
bHigh.OnEvent("Click", (*) => SetMode(false))
(isEco ? bEco : bHigh).Enabled := false
g.OnEvent("Escape", (*) => ExitApp())
g.OnEvent("Close", (*) => ExitApp())
g.Show()
(isEco ? bHigh : bEco).Focus()

SetMode(eco, quiet := false) {
    if eco {
        RunWait(Format("powercfg /setacvalueindex {} {} {} 0", SAVER, PROC, BOOST), , "Hide")
        RunWait("powercfg /setactive " SAVER, , "Hide")
    } else
        RunWait("powercfg /setactive " HIGH, , "Hide")
    ok := GetActiveScheme() = (eco ? SAVER : HIGH)
    if !ok && !quiet
        MsgBox("Не удалось переключить режим питания.", "Режим питания", "Iconx")
    ExitApp(ok ? 0 : 1)
}

GetActiveScheme() {
    pGuid := 0
    if DllCall("powrprof\PowerGetActiveScheme", "Ptr", 0, "Ptr*", &pGuid, "UInt")
        return ""
    buf := Buffer(80)
    DllCall("ole32\StringFromGUID2", "Ptr", pGuid, "Ptr", buf, "Int", 40)
    DllCall("LocalFree", "Ptr", pGuid)
    return StrLower(Trim(StrGet(buf), "{}"))
}
