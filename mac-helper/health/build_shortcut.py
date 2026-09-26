"""Generate the "Health Export" shortcut (unsigned XML plist).

For every Health type: Find Health Samples (Type is X, Start Date after a cutoff), then Get Details for Start Date, End Date,
Value, Unit and Source on the whole list at once (no per-sample loop, so thousands of heart-rate samples stay fast), each
combined with new lines, then one text section per type. The file goes to iCloud Drive/Shortcuts/Health/health-<stamp>.txt.
The header carries the automation's input (an "hq:" request id) and the generation time, both ISO 8601.
"""
import plistlib
import sys
import uuid

OBJ = "￼"

# Find Health Samples picker labels: Steps, Sleep, Active Calories, Exercise Minutes and Walking + Running Distance are
# confirmed from real exports; the rest are HealthKit's own display names (Localizable-DataTypes on this Mac).
TYPES_2D = ["Steps", "Heart Rate", "Active Calories", "Walking + Running Distance", "Resting Heart Rate",
            "Heart Rate Variability", "Walking Heart Rate Average", "Respiratory Rate", "Sleep"]
# Only types the Watch and iPhone record every day: Find Health Samples throws on a type with no samples in the window and
# Shortcuts cannot catch it. Types that can be empty for days (Exercise Minutes, Weight, Body Fat, Walking Steadiness,
# Headphone Audio Levels) come from the full Health export instead. Sleep goes last: a sleepless night is the likeliest gap.
TYPES_1D = []
# High-volume types are grouped by hour on the iPhone (totals for cumulative types, averages for heart rate and sound):
# at most 48 values each over two days instead of thousands of samples. Heart rate is NOT grouped: Shortcuts sums a discrete
# type per hour (3910 "bpm"), so it is read as raw samples for the last day, newest 400.
DAILY = {"Resting Heart Rate", "Walking Heart Rate Average", "Heart Rate Variability", "Respiratory Rate", "Blood Oxygen",
         "Weight", "Body Fat Percentage", "Walking Steadiness"}
HOURLY = {"Steps", "Active Calories", "Exercise Minutes", "Walking + Running Distance", "Flights Climbed", "Time In Daylight",
          "Environmental Sound Levels", "Headphone Audio Levels"}
PROPS = ["Start Date", "End Date", "Value", "Unit", "Source"]


def uid():
    return str(uuid.uuid4()).upper()


def out(u, name):
    return {"OutputName": name, "OutputUUID": u, "Type": "ActionOutput"}


def token_string(parts):
    """parts: list of str or attachment dicts -> WFTextTokenString."""
    s, att = "", {}
    for p in parts:
        if isinstance(p, str):
            s += p
        else:
            att["{%d, 1}" % len(s)] = p
            s += OBJ
    return {"Value": {"attachmentsByRange": att, "string": s}, "WFSerializationType": "WFTextTokenString"}


def token_attachment(att):
    return {"Value": att, "WFSerializationType": "WFTextTokenAttachment"}


def action(ident, **params):
    return {"WFWorkflowActionIdentifier": "is.workflow.actions." + ident, "WFWorkflowActionParameters": params}


actions = []


def add(ident, name=None, **params):
    u = uid()
    actions.append(action(ident, UUID=u, **params))
    return u


def iso(date_output_uuid, name):
    return add("format.date", WFDate=token_string([out(date_output_uuid, name)]), WFDateFormatStyle="ISO 8601",
               WFISO8601IncludeTime=True)


def comment(text):
    actions.append(action("comment", UUID=uid(), WFCommentActionText=text))


comment("Health Export: writes the last two days of Apple Health data (the last day for heart rate) to iCloud Drive, "
        "Shortcuts/Health, for the iCloud MCP server on the Mac. Run by automations; nothing is shown or sent.")
comment("Input: an automation's text. A message containing hq: asks for fresh data now; its text is copied into the file "
        "header so the server can match the answer to its request.")
comment("Dates and cut-offs: now, now minus two days, now minus one day.")
# ---- header and cut-off dates
now = add("date", WFDateActionMode="Current Date")
now_iso = iso(now, "Date")
stamp = add("format.date", WFDate=token_string([out(now, "Date")]), WFDateFormatStyle="Custom", WFDateFormat="yyyyMMdd-HHmmss")
since2 = add("adjustdate", WFAdjustOperation="Subtract", WFDate=token_string([out(now, "Date")]),
             WFDuration={"Value": {"Magnitude": "2", "Unit": "days"}, "WFSerializationType": "WFQuantityFieldValue"})
since1 = add("adjustdate", WFAdjustOperation="Subtract", WFDate=token_string([out(now, "Date")]),
             WFDuration={"Value": {"Magnitude": "1", "Unit": "days"}, "WFSerializationType": "WFQuantityFieldValue"})
# ---- at most one export per clock hour: automations fire on every message from Claude, so a chat would otherwise export each time
comment("Once per hour: stop if this hour already has an export (Health/last-hour.txt holds the hour of the last one).")
hour = add("format.date", WFDate=token_string([out(now, "Date")]), WFDateFormatStyle="Custom", WFDateFormat="yyyyMMddHH")
last = add("documentpicker.open", WFFileErrorIfNotFound=False, WFShowFilePicker=False,
           WFGetFilePath=token_string(["Health/last-hour.txt"]))
last_text = add("gettext", WFTextActionText=token_string([out(last, "File")]))
gate = uid()
actions.append(action("conditional", GroupingIdentifier=gate, WFControlFlowMode=0, WFCondition=99,
                      WFConditionalActionString=token_string([out(hour, "Formatted Date")]),
                      WFInput={"Type": "Variable", "Variable": token_attachment(out(last_text, "Text"))}))
actions.append(action("exit", UUID=uid()))
actions.append(action("conditional", GroupingIdentifier=gate, WFControlFlowMode=2))
header = add("gettext", WFTextActionText=token_string(
    ["#health v1 generated=", out(now_iso, "Formatted Date"), " input=", {"Type": "ExtensionInput"}]))
path = ["Health/health-", out(stamp, "Formatted Date"), ".txt"]
actions.append(action("documentpicker.save", UUID=uid(), WFInput=token_attachment(out(header, "Text")), WFAskWhereToSave=False,
                      WFSaveFileOverwrite=False, WFFileDestinationPath=token_string(path)))

comment("The file is created first; each Health type is appended as it is read. One section per type: the samples' start, end, value, unit and source as columns, one line per sample.")
# ---- one section per Health type
for label, since in [(t, since2) for t in TYPES_2D] + [(t, since1) for t in TYPES_1D]:
    # Find Health Samples throws when a type has no samples, and Shortcuts cannot catch errors, so one empty type would stop
    # the run. Grouping with Fill Missing on returns a zero for every empty hour or day instead: never empty. Only Sleep (a
    # category, which cannot be grouped) is read as raw samples; with a Watch the last two days always have some.
    if label in HOURLY:
        grouping = {"WFHKSampleFilteringGroupBy": "Hour", "WFHKSampleFilteringFillMissing": True}
    elif label in DAILY:
        grouping = {"WFHKSampleFilteringGroupBy": "Day", "WFHKSampleFilteringFillMissing": True}
    else:
        grouping = {}
    # Bounded twice: "Start Date is in the last 2 days" (a fixed filter, no variables) and a cap on the number of results,
    # newest first, so a query can never read years of history even if a filter did not import.
    find = add("filter.health.quantity", WFContentItemLimitEnabled=True, WFContentItemLimitNumber=60 if label in HOURLY else 5 if label in DAILY else 400 if label == "Heart Rate" else 300,
               WFContentItemSortProperty="Start Date", WFContentItemSortOrder="Latest First", **grouping, WFContentItemFilter={
        "Value": {"WFActionParameterFilterPrefix": 1, "WFContentPredicateBoundedDate": False,
                  "WFActionParameterFilterTemplates": [
                      {"Bounded": True, "Operator": 4, "Property": "Type", "Removable": False,
                       "Values": {"Enumeration": {"Value": label, "WFSerializationType": "WFStringSubstitutableState"}}},
                      {"Operator": 1001, "Property": "Start Date", "Removable": True,
                       "Values": {"Number": "1" if label == "Heart Rate" else "2", "Unit": 16}},
                  ]},
        "WFSerializationType": "WFContentPredicateTableTemplate"})
    # A type with no samples in the window makes Get Details fail and would stop the whole shortcut: count first, skip empties.
    count = add("count", WFCountType="Items", Input=token_attachment(out(find, "Health Samples")),
                WFInput=token_attachment(out(find, "Health Samples")))
    group = uid()
    comment("Only when %s has samples in the window." % label)
    actions.append(action("conditional", GroupingIdentifier=group, WFControlFlowMode=0, WFCondition=2, WFNumberValue="0",
                          WFInput={"Type": "Variable", "Variable": token_attachment(out(count, "Count"))}))
    columns = []
    for prop in PROPS:
        detail = add("properties.health.quantity", WFContentItemPropertyName=prop,
                     WFInput=token_attachment(out(find, "Health Samples")))
        src, src_name = detail, prop
        if prop.endswith("Date"):
            src, src_name = iso(detail, prop), "Formatted Date"
        combined = add("text.combine", WFTextSeparator="New Lines", text=token_attachment(out(src, src_name)))
        columns.append(combined)
    parts = ["##type=%s%s\n##start\n" % (label, " grouped=hour" if label in HOURLY else " grouped=day" if label in DAILY else ""), out(columns[0], "Combined Text"), "\n##end\n", out(columns[1], "Combined Text"),
             "\n##value\n", out(columns[2], "Combined Text"), "\n##unit\n", out(columns[3], "Combined Text"),
             "\n##source\n", out(columns[4], "Combined Text")]
    section = add("gettext", WFTextActionText=token_string(parts))
    # Appended as soon as it is read: if a later type fails, everything before it is already saved.
    actions.append(action("file.append", UUID=uid(), WFFilePath=token_string(path), WFAppendOnNewLine=True,
                          WFAppendFileWriteMode="Append", WFInput=token_string([out(section, "Text")])))
    if label == TYPES_2D[0]:
        # the hour is marked done only once Health could be read: a run on a locked phone (Health locked, nothing found) or before
        # Health access was granted leaves the hour open for the next try
        hour_text = add("gettext", WFTextActionText=token_string([out(hour, "Formatted Date")]))
        actions.append(action("documentpicker.save", UUID=uid(), WFInput=token_attachment(out(hour_text, "Text")),
                              WFAskWhereToSave=False, WFSaveFileOverwrite=True,
                              WFFileDestinationPath=token_string(["Health/last-hour.txt"])))
    actions.append(action("conditional", GroupingIdentifier=group, WFControlFlowMode=2))

workflow = {
    "WFWorkflowActions": actions,
    "WFWorkflowClientVersion": "2700.0.4",
    "WFWorkflowHasOutputFallback": False,
    "WFWorkflowIcon": {"WFWorkflowIconGlyphNumber": 59446, "WFWorkflowIconStartColor": 4282601983},
    "WFWorkflowImportQuestions": [],
    "WFWorkflowInputContentItemClasses": ["WFStringContentItem"],
    "WFWorkflowMinimumClientVersion": 900,
    "WFWorkflowMinimumClientVersionString": "900",
    "WFWorkflowName": "Health Export",
    "WFWorkflowOutputContentItemClasses": [],
    "WFWorkflowTypes": [],
}
with open(sys.argv[1], "wb") as f:
    plistlib.dump(workflow, f, fmt=plistlib.FMT_XML)
print(len(actions), "actions")
