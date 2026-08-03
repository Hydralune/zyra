import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type KeyboardEvent,
} from "react"
import type { WorkbenchRuntime } from "../../app/runtime.ts"
import type { TaskProjection } from "../../../../../packages/core/typed-api-client/src/index.ts"
import { useCommandSnapshot, useQueueSnapshot, useWorkbenchSnapshot } from "../../app/hooks.ts"
import { applyCompletion, commandArgumentHint, completionContext } from "../../command/parser.ts"
import { commandUsage, type CommandSuggestion } from "../../command/catalog.ts"
import {
  decideCommandKey,
  selectedSuggestionIndex,
} from "../../command/keyboard.ts"
import {
  applyArgumentSuggestion,
  argumentSuggestions,
  type ArgumentSuggestion,
} from "../../command/argument-completion.ts"
import { CommandQueuePanel } from "../../features/commands/queue-panel.tsx"
import {
  classifyPermissionSealedManualAction,
  permissionDisplayActor,
  rejectPermissionSealedManualAction,
} from "../../features/permissions/index.ts"
import type { PaletteEntry } from "../../../../../packages/commands/src/index.ts"

function cursorAt(textarea: HTMLTextAreaElement): number {
  return textarea.selectionStart ?? textarea.value.length
}

function setSelection(textarea: HTMLTextAreaElement, position: number): void {
  requestAnimationFrame(() => {
    textarea.focus({ preventScroll: true })
    textarea.setSelectionRange(position, position)
  })
}

function SuggestionList({
  suggestions,
  selected,
  onSelected,
  onChoose,
}: {
  suggestions: readonly CommandSuggestion[]
  selected: number
  onSelected: (index: number) => void
  onChoose: (suggestion: CommandSuggestion) => void
}) {
  if (!suggestions.length) {
    return (
      <div className="command-suggestions command-suggestions-empty" role="status">
        No matching commands
      </div>
    )
  }
  return (
    <div
      className="command-suggestions"
      id="command-suggestions"
      role="listbox"
      aria-label="Slash command suggestions"
    >
      {suggestions.map((suggestion, index) => {
        const definition = suggestion.definition
        return (
          <button
            key={definition.id}
            id={`suggestion-${definition.id}`}
            className="command-suggestion"
            type="button"
            role="option"
            aria-selected={index === selected}
            disabled={!suggestion.availability.enabled}
            onMouseMove={() => onSelected(index)}
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => onChoose(suggestion)}
          >
            <span className="suggestion-command">/{definition.trigger}</span>
            <span className="suggestion-copy">
              <strong>{definition.title}</strong>
              <span>{definition.description}</span>
            </span>
            <span className="suggestion-badges">
              {definition.remoteSafe ? <span className="tag">remote safe</span> : <span className="tag tag-muted">local</span>}
              {!suggestion.availability.enabled ? (
                <span className="tag tag-danger">{suggestion.availability.reason}</span>
              ) : null}
            </span>
          </button>
        )
      })}
    </div>
  )
}

function ArgumentSuggestionList({
  suggestions,
  selected,
  onSelected,
  onChoose,
}: {
  suggestions: readonly ArgumentSuggestion[]
  selected: number
  onSelected: (index: number) => void
  onChoose: (suggestion: ArgumentSuggestion) => void
}) {
  return (
    <div
      className="command-suggestions argument-suggestions"
      id="command-argument-suggestions"
      role="listbox"
      aria-label="Command argument suggestions"
    >
      {suggestions.map((suggestion, index) => (
        <button
          key={suggestion.id}
          id={`argument-${suggestion.id.replace(/[^a-z0-9_-]/gi, "-")}`}
          className="command-suggestion"
          type="button"
          role="option"
          aria-selected={selected === index}
          disabled={suggestion.disabled}
          onMouseMove={() => onSelected(index)}
          onMouseDown={(event) => event.preventDefault()}
          onClick={() => onChoose(suggestion)}
        >
          <span className="suggestion-command">{suggestion.value}</span>
          <span className="suggestion-copy">
            <strong>{suggestion.label}</strong>
            <span>{suggestion.description}</span>
          </span>
          <span className="tag tag-muted">{suggestion.kind}</span>
        </button>
      ))}
    </div>
  )
}

function ControlArgumentList({
  entries,
  selected,
  onSelected,
  onChoose,
}: {
  entries: readonly PaletteEntry[]
  selected: number
  onSelected: (entry: PaletteEntry) => void
  onChoose: (entry: PaletteEntry) => void
}) {
  return (
    <div
      className="command-suggestions argument-suggestions"
      id="control-command-argument-suggestions"
      role="listbox"
      aria-label="Typed control command argument suggestions"
      data-command-palette-owner="packages/commands/CommandPalette"
    >
      {entries.map((entry, index) => (
        <button
          key={entry.id}
          id={`control-argument-${entry.id.replace(/[^a-z0-9_-]/gi, "-")}`}
          className="command-suggestion"
          type="button"
          role="option"
          aria-selected={selected === index}
          disabled={entry.disabled}
          onMouseMove={() => onSelected(entry)}
          onMouseDown={(event) => event.preventDefault()}
          onClick={() => onChoose(entry)}
        >
          <span className="suggestion-command">{entry.value}</span>
          <span className="suggestion-copy">
            <strong>{entry.label}</strong>
            <span>{entry.description}</span>
          </span>
          <span className="tag tag-muted">{entry.kind}</span>
        </button>
      ))}
    </div>
  )
}

function QueuePreview({
  runtime,
  selectedTask,
}: {
  runtime: WorkbenchRuntime
  selectedTask?: TaskProjection
}) {
  const queue = useQueueSnapshot(runtime)
  const visible = queue.visible.slice(0, 8)
  if (!visible.length) return null
  return (
    <section className="queue-preview" aria-labelledby="queue-preview-heading">
      <header>
        <span id="queue-preview-heading">
          Queued input <strong>{queue.pendingCount}</strong>
        </span>
        <button
          type="button"
          onClick={() => runtime.queue.removeSettled()}
          disabled={!queue.entries.some((entry) => ["committed", "failed", "cancelled"].includes(entry.phase))}
        >
          Clear settled
        </button>
      </header>
      <ol>
        {visible.map((entry) => (
          <li key={entry.id} data-phase={entry.phase}>
            <span className={`queue-phase queue-phase-${entry.phase}`} aria-hidden="true" />
            <span className="queue-value">{entry.value}</span>
            <span className="queue-meta">{entry.priority} · {entry.phase}</span>
            {entry.phase === "queued" && entry.editable ? (
              <button
                type="button"
                onClick={() => {
                  const popped = runtime.queue.popEditable("", 0)
                  if (!popped) return
                  window.dispatchEvent(new CustomEvent("zyra:restore-command-draft", {
                    detail: { value: popped.value, cursor: popped.cursor },
                  }))
                }}
              >
                Edit
              </button>
            ) : null}
            {entry.phase === "failed" || entry.phase === "cancelled" ? (
              <button type="button" onClick={() => {
                void (async () => {
                  const rejected = await rejectPermissionSealedManualAction({
                    runtime: runtime.permissionConsole,
                    action: "retry",
                    actorId: permissionDisplayActor(selectedTask?.metadata),
                    requestId: entry.id,
                    reason:
                      "A manual queued-input retry cannot advance a sealed run.",
                  })
                  if (rejected) return
                  runtime.queue.retry(entry.id)
                  await runtime.commands.drain()
                })()
              }}>
                Retry
              </button>
            ) : null}
          </li>
        ))}
      </ol>
      {queue.visible.length > visible.length ? (
        <p className="queue-overflow">{queue.visible.length - visible.length} more queued items</p>
      ) : null}
    </section>
  )
}

export function CommandInput({
  runtime,
  taskContext,
}: {
  runtime: WorkbenchRuntime
  taskContext?: TaskProjection
}) {
  const command = useCommandSnapshot(runtime)
  const control = useSyncExternalStore(
    runtime.controlCommands.subscribe,
    runtime.controlCommands.getSnapshot,
    runtime.controlCommands.getSnapshot,
  )
  const inputBusy = command.busy || control.coordinator.busy
  const workbench = useWorkbenchSnapshot(runtime)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const [value, setValue] = useState("")
  const [cursor, setCursor] = useState(0)
  const [selectedSuggestion, setSelectedSuggestion] = useState(0)
  const [suggestionsDismissed, setSuggestionsDismissed] = useState(false)
  const [submissionError, setSubmissionError] = useState<string>()
  const selectedTask = taskContext
  const parsed = useMemo(() => runtime.commands.parse(value), [runtime, value])
  const completion = useMemo(
    () => completionContext(value, cursor, runtime.catalog),
    [runtime, value, cursor],
  )
  const suggestions = useMemo(() => {
    if (completion.kind !== "command") return []
    return runtime.catalog.search(completion.query, runtime.commands.context(), 10)
  }, [runtime, completion, workbench.revision, command.revision])
  const argumentOptions = useMemo(
    () => argumentSuggestions({
      parsed,
      completion,
      catalog: runtime.catalog,
      tasks: workbench.list.tasks,
      limit: 10,
    }),
    [completion, parsed, runtime, workbench.list.tasks],
  )
  const showSuggestions =
    !suggestionsDismissed &&
    completion.kind === "command" &&
    value.trimStart().startsWith("/")
  const showArgumentSuggestions =
    !suggestionsDismissed &&
    completion.kind === "argument" &&
    argumentOptions.length > 0
  const suggestionsOpen = showSuggestions || showArgumentSuggestions
  const argumentHint = useMemo(
    () => commandArgumentHint(parsed, cursor),
    [parsed, cursor],
  )
  const showControlArgumentSuggestions =
    !suggestionsDismissed &&
    Boolean(control.palette.parsed.descriptor) &&
    control.palette.completion.kind !== "command" &&
    control.palette.entries.length > 0
  const effectiveSuggestionsOpen =
    suggestionsOpen || showControlArgumentSuggestions

  useEffect(() => {
    runtime.controlCommands.input.update({ value, cursor })
  }, [cursor, runtime, value])

  useEffect(() => {
    const count = showArgumentSuggestions ? argumentOptions.length : suggestions.length
    if (selectedSuggestion >= count) setSelectedSuggestion(0)
  }, [argumentOptions.length, selectedSuggestion, showArgumentSuggestions, suggestions.length])

  const draftScope = selectedTask?.taskId ?? "new"
  useEffect(() => {
    const draft = runtime.drafts.get(draftScope)
    setValue(draft?.value ?? "")
    const position = draft?.cursor ?? 0
    setCursor(position)
    if (draft?.value && textareaRef.current) setSelection(textareaRef.current, position)
  }, [draftScope, runtime])

  useEffect(() => {
    const restore = (event: Event) => {
      const detail = (event as CustomEvent<{ value?: string; cursor?: number }>).detail
      if (!detail?.value) return
      setValue(detail.value)
      const position = detail.cursor ?? detail.value.length
      setCursor(position)
      if (textareaRef.current) setSelection(textareaRef.current, position)
    }
    window.addEventListener("zyra:restore-command-draft", restore)
    return () => window.removeEventListener("zyra:restore-command-draft", restore)
  }, [])

  const chooseSuggestion = useCallback((suggestion: CommandSuggestion) => {
    if (!suggestion.availability.enabled) return
    const applied = applyCompletion(value, completion, suggestion.definition.trigger)
    setValue(applied.value)
    runtime.drafts.set(draftScope, applied.value, applied.cursor)
    setCursor(applied.cursor)
    setSelectedSuggestion(0)
    if (textareaRef.current) setSelection(textareaRef.current, applied.cursor)
  }, [completion, draftScope, runtime, value])

  const chooseArgument = useCallback((suggestion: ArgumentSuggestion) => {
    const applied = applyArgumentSuggestion(value, completion, suggestion)
    setValue(applied.value)
    runtime.drafts.set(draftScope, applied.value, applied.cursor)
    setCursor(applied.cursor)
    setSelectedSuggestion(0)
    if (textareaRef.current) setSelection(textareaRef.current, applied.cursor)
  }, [completion, draftScope, runtime, value])

  const chooseControlArgument = useCallback((entry: PaletteEntry) => {
    runtime.controlCommands.palette.select(entry.id)
    const applied = runtime.controlCommands.palette.apply(value)
    if (!applied) return
    setValue(applied.value)
    runtime.drafts.set(draftScope, applied.value, applied.cursor)
    setCursor(applied.cursor)
    if (textareaRef.current) setSelection(textareaRef.current, applied.cursor)
  }, [draftScope, runtime, value])

  const submit = useCallback(async (
    origin: "keyboard" | "button" = "keyboard",
    controlMode: "enqueue" | "steer" | "interrupt" = "enqueue",
  ) => {
    const captured = value
    if (!captured.trim() || !command.enabled) return
    const sealedAction = classifyPermissionSealedManualAction({
      value: captured,
      deliveryMode: controlMode,
    })
    if (
      sealedAction
      && runtime.permissionConsole.getSnapshot().productMode === "sealed"
    ) {
      setSubmissionError(undefined)
      try {
        const rejected = await rejectPermissionSealedManualAction({
          runtime: runtime.permissionConsole,
          action: sealedAction,
          actorId: permissionDisplayActor(selectedTask?.metadata),
          reason:
            `Manual ${sealedAction.replace("_", " ")} cannot advance a sealed run.`,
        })
        setSubmissionError(
          rejected?.reason
            ?? "The sealed permission policy rejected this manual control.",
        )
      } catch (error) {
        setSubmissionError(
          error instanceof Error ? error.message : String(error),
        )
      }
      return
    }
    const capture = runtime.drafts.captureValue(
      draftScope,
      captured,
      textareaRef.current ? cursorAt(textareaRef.current) : captured.length,
    )
    setSubmissionError(undefined)
    setValue("")
    setCursor(0)
    runtime.drafts.clear(draftScope)
    try {
      await runtime.commands.submit(captured, {
        origin,
        taskId: selectedTask?.taskId,
        runId: selectedTask?.runId,
        sessionId: selectedTask?.sessionId,
        taskStatus: selectedTask?.status,
        taskActive: selectedTask?.active,
        taskTerminal: selectedTask?.terminal,
        allowQueue: true,
        controlMode,
      })
      runtime.drafts.commit(capture.id)
      runtime.history.resetNavigation()
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      setSubmissionError(message)
      const restored = runtime.drafts.restore(capture.id)
      if (restored) {
        setValue(restored.value)
        setCursor(restored.cursor)
        if (textareaRef.current) setSelection(textareaRef.current, restored.cursor)
      }
    }
  }, [command.enabled, draftScope, runtime, selectedTask, value])

  const navigateHistory = useCallback((direction: "up" | "down") => {
    const textarea = textareaRef.current
    if (!textarea) return false
    const navigation = runtime.history.navigate(
      direction,
      value,
      cursorAt(textarea),
      { taskId: selectedTask?.taskId },
    )
    if (!navigation.handled || navigation.value === undefined) return false
    const position = navigation.cursor === "start" ? 0 : navigation.value.length
    setValue(navigation.value)
    runtime.drafts.set(draftScope, navigation.value, position)
    setCursor(position)
    setSelection(textarea, position)
    return true
  }, [draftScope, runtime, selectedTask?.taskId, value])

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    const decision = decideCommandKey({
      key: event.key,
      shift: event.shiftKey,
      alt: event.altKey,
      ctrl: event.ctrlKey,
      meta: event.metaKey,
      composing: event.nativeEvent.isComposing,
      suggestionsOpen: effectiveSuggestionsOpen,
      suggestionCount: showControlArgumentSuggestions
        ? control.palette.entries.length
        : showArgumentSuggestions
          ? argumentOptions.length
          : suggestions.length,
      value,
      cursor: cursorAt(event.currentTarget),
      busy: inputBusy,
      overlayOpen: Boolean(runtime.overlays.active()),
      editableQueuedCount: runtime.queue.list({
        phases: ["queued"],
      }).filter((entry) => entry.editable).length,
      inHistory: false,
    })
    if (decision.preventDefault) event.preventDefault()
    if (decision.stopPropagation) event.stopPropagation()
    if (decision.action === "submit") {
      if (
        showControlArgumentSuggestions &&
        control.palette.entries[control.palette.selectedIndex]
      ) {
        chooseControlArgument(
          control.palette.entries[control.palette.selectedIndex]!,
        )
      } else if (showArgumentSuggestions && argumentOptions[selectedSuggestion]) {
        chooseArgument(argumentOptions[selectedSuggestion]!)
      } else if (showSuggestions && suggestions[selectedSuggestion]?.availability.enabled) {
        chooseSuggestion(suggestions[selectedSuggestion]!)
      } else {
        void submit(
          "keyboard",
          event.altKey && (event.ctrlKey || event.metaKey)
            ? "interrupt"
            : event.altKey
              ? "steer"
              : "enqueue",
        )
      }
      return
    }
    if (decision.action === "close-suggestions") {
      setSuggestionsDismissed(true)
      return
    }
    if (decision.action === "cancel" || decision.action === "restore-queue") {
      if (runtime.overlays.handleEscape()) return
      if (runtime.controlCommands.cancelActive()) return
      if (inputBusy && runtime.commands.cancelActive()) return
      const popped = runtime.queue.popEditable(value, cursorAt(event.currentTarget))
      if (popped) {
        setValue(popped.value)
        setCursor(popped.cursor)
        runtime.drafts.set(draftScope, popped.value, popped.cursor)
        setSelection(event.currentTarget, popped.cursor)
      }
      return
    }
    if (decision.action === "suggestion-next") {
      if (showControlArgumentSuggestions) {
        runtime.controlCommands.palette.move("next")
        return
      }
      setSelectedSuggestion((current) =>
        selectedSuggestionIndex(
          current,
          showArgumentSuggestions ? argumentOptions.length : suggestions.length,
          "next",
        ),
      )
      return
    }
    if (decision.action === "suggestion-previous") {
      if (showControlArgumentSuggestions) {
        runtime.controlCommands.palette.move("previous")
        return
      }
      setSelectedSuggestion((current) =>
        selectedSuggestionIndex(
          current,
          showArgumentSuggestions ? argumentOptions.length : suggestions.length,
          "previous",
        ),
      )
      return
    }
    if (decision.action === "history-previous" && navigateHistory("up")) {
      return
    }
    if (decision.action === "history-next") {
      navigateHistory("down")
    }
  }

  const activeDescription =
    showControlArgumentSuggestions &&
    control.palette.entries[control.palette.selectedIndex]
      ? `control-argument-${control.palette.entries[control.palette.selectedIndex]!.id.replace(/[^a-z0-9_-]/gi, "-")}`
      : showArgumentSuggestions && argumentOptions[selectedSuggestion]
      ? `argument-${argumentOptions[selectedSuggestion]!.id.replace(/[^a-z0-9_-]/gi, "-")}`
      : suggestions[selectedSuggestion]
        ? `suggestion-${suggestions[selectedSuggestion]!.definition.id}`
        : undefined
  const disabled = !command.enabled || !workbench.transportEnabled
  const contextLabel = !selectedTask
    ? "新任务"
    : selectedTask.active
      ? "当前会话 · 正在执行"
      : selectedTask.terminal
        ? "当前会话 · 可继续提问"
        : "当前会话"
  const placeholder = selectedTask?.active
    ? "补充要求，新消息会排在当前任务之后"
    : selectedTask?.terminal
      ? "继续此会话，或输入 / 查看命令"
      : "描述一个任务，或向 Zyra 提问"
  return (
    <footer className="command-dock">
      <QueuePreview runtime={runtime} selectedTask={selectedTask} />
      <div className="command-input-wrap" data-busy={command.busy || undefined}>
        {showSuggestions ? (
          <SuggestionList
            suggestions={suggestions}
            selected={selectedSuggestion}
            onSelected={setSelectedSuggestion}
            onChoose={chooseSuggestion}
          />
        ) : null}
        {showControlArgumentSuggestions ? (
          <ControlArgumentList
            entries={control.palette.entries}
            selected={control.palette.selectedIndex}
            onSelected={(entry) => {
              runtime.controlCommands.palette.select(entry.id)
            }}
            onChoose={chooseControlArgument}
          />
        ) : showArgumentSuggestions ? (
          <ArgumentSuggestionList
            suggestions={argumentOptions}
            selected={selectedSuggestion}
            onSelected={setSelectedSuggestion}
            onChoose={chooseArgument}
          />
        ) : null}
        <div className="command-context">
          <span className={`status-marker ${selectedTask ? `status-${selectedTask.status}` : "status-idle"}`} aria-hidden="true" />
          <span>{contextLabel}</span>
          {selectedTask ? <span className="command-context-title">{selectedTask.userGoal || selectedTask.taskId}</span> : null}
          {inputBusy ? <span className="tag">正在执行 · 新指令将进入任务队列</span> : null}
        </div>
        <div className="command-editor">
          <textarea
            ref={textareaRef}
            id="workbench-command-input"
            value={value}
            rows={1}
            placeholder={placeholder}
            aria-label="Zyra 任务输入"
            aria-expanded={effectiveSuggestionsOpen}
            aria-controls={
              showSuggestions
                ? "command-suggestions"
                : showControlArgumentSuggestions
                  ? "control-command-argument-suggestions"
                : showArgumentSuggestions
                  ? "command-argument-suggestions"
                  : undefined
            }
            aria-activedescendant={effectiveSuggestionsOpen ? activeDescription : undefined}
            aria-describedby={argumentHint ? "command-argument-hint" : undefined}
            disabled={disabled}
            onChange={(event) => {
              setValue(event.target.value)
              const position = cursorAt(event.target)
              setCursor(position)
              runtime.drafts.set(draftScope, event.target.value, position)
              setSuggestionsDismissed(false)
              setSubmissionError(undefined)
            }}
            onCompositionStart={() => {
              runtime.controlCommands.input.compositionStart()
            }}
            onCompositionEnd={(event) => {
              runtime.controlCommands.input.compositionEnd(
                event.currentTarget.value,
                cursorAt(event.currentTarget),
              )
            }}
            onClick={(event) => setCursor(cursorAt(event.currentTarget))}
            onKeyUp={(event) => setCursor(cursorAt(event.currentTarget))}
            onKeyDown={handleKeyDown}
          />
          <button
            className="command-submit"
            type="button"
            disabled={disabled || !value.trim()}
            aria-label={inputBusy ? "将指令加入队列" : "发送任务"}
            onClick={() => void submit("button")}
          >
            {inputBusy ? "排队" : "发送"}
          </button>
        </div>
        <div className="command-footer" aria-live="polite">
          <span id="command-argument-hint">
            {argumentHint ??
              (parsed.kind === "command" && parsed.definition
                ? commandUsage(parsed.definition)
                : "Enter 发送 · Shift+Enter 换行 · 输入 / 查看命令")}
          </span>
          <span>{value.length.toLocaleString()} chars</span>
        </div>
        <CommandQueuePanel
          runtime={runtime.controlCommands}
          permissionRuntime={runtime.permissionConsole}
          actorId={permissionDisplayActor(selectedTask?.metadata)}
        />
        {submissionError || command.lastError ? (
          <div className="command-error" role="alert">
            {submissionError ?? command.lastError}
          </div>
        ) : null}
      </div>
    </footer>
  )
}
