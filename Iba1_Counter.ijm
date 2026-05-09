// =============================================================================
// Iba1Counter.ijm
//
// Fiji macro front-end for the Iba1 microglia quantification pipeline.
// All workflow steps (setup, ROI drawing, running Python, QC review, manual
// correction) are exposed via a single Plugins menu entry. The Python
// pipeline (analyze_iba1_microglia.py) runs in the background via exec().
//
// Install:
//   Copy this file to <Fiji.app>/plugins/Iba1Counter.ijm and restart Fiji.
//   It will then appear as Plugins > Iba1Counter.
//
// First run:
//   Tick "Configure paths" in the main menu and point the macro at your
//   Python executable and at analyze_iba1_microglia.py. These settings
//   persist across Fiji restarts in ~/.iba1counter_prefs.txt.
// =============================================================================


// =============================================================================
// Settings — file-backed key=value store at ~/.iba1counter_prefs.txt
//
// We deliberately AVOID ij.Prefs.{set,getString} via call(): on this Fiji
// build the values written via call("ij.Prefs.set", ...) failed to round-trip
// through call("ij.Prefs.getString", ...) within the same session, so
// settings appeared "(not set)" in the menu even immediately after Configure.
// File I/O bypasses every reflection / overload-resolution / key-prefix
// quirk, and the user can open the file in Notepad to inspect/edit it.
// =============================================================================

function settingsFilePath() {
    return getDirectory("home") + ".iba1counter_prefs.txt";
}

function readSetting(key, defaultVal) {
    p = settingsFilePath();
    if (!File.exists(p)) return defaultVal;
    text = File.openAsString(p);
    lines = split(text, "\n");
    for (i = 0; i < lines.length; i++) {
        line = lines[i];
        // Tolerate Windows CRLF on a per-line basis.
        if (endsWith(line, "\r")) line = substring(line, 0, lengthOf(line) - 1);
        eqIdx = indexOf(line, "=");
        if (eqIdx > 0) {
            k = substring(line, 0, eqIdx);
            if (k == key) return substring(line, eqIdx + 1);
        }
    }
    return defaultVal;
}

function writeSetting(key, val) {
    p = settingsFilePath();
    out = "";
    found = false;
    if (File.exists(p)) {
        text = File.openAsString(p);
        lines = split(text, "\n");
        for (i = 0; i < lines.length; i++) {
            line = lines[i];
            if (endsWith(line, "\r")) line = substring(line, 0, lengthOf(line) - 1);
            if (lengthOf(trim(line)) == 0) continue;
            eqIdx = indexOf(line, "=");
            if (eqIdx > 0 && substring(line, 0, eqIdx) == key) {
                if (lengthOf(out) > 0) out = out + "\n";
                out = out + key + "=" + val;
                found = true;
            } else {
                if (lengthOf(out) > 0) out = out + "\n";
                out = out + line;
            }
        }
    }
    if (!found) {
        if (lengthOf(out) > 0) out = out + "\n";
        out = out + key + "=" + val;
    }
    File.saveString(out, p);
}

// IMPORTANT: do NOT introduce one-line wrapper functions like
//   function getX() { return readSetting("foo", ""); }
// IJM's return-type inference fails on this layered string-returning pattern
// and reports a misleading "Numeric return value expected" error at the
// wrapper's body. Call readSetting / writeSetting directly at every site.


// =============================================================================
// Path / file helpers
// =============================================================================

function fwdSlash(p) {
    // YAML-safe, Python-safe path representation.
    return replace(p, "\\", "/");
}

function stripOuterQuotes(s) {
    s = trim(s);
    if (lengthOf(s) >= 2) {
        first = substring(s, 0, 1);
        last = substring(s, lengthOf(s) - 1);
        if (first == "\"" && last == "\"") {
            return substring(s, 1, lengthOf(s) - 1);
        }
    }
    return s;
}

function ensureTrailingSeparator(p) {
    if (endsWith(p, File.separator) || endsWith(p, "/")) return p;
    return p + File.separator;
}

function ensureDir(p) {
    if (!File.exists(p)) File.makeDirectory(p);
}

function closeIfOpen(title) {
    if (isOpen(title)) {
        selectWindow(title);
        close();
    }
}

function channelColorName(chanNumber) {
    if (chanNumber == 1) return "red";
    if (chanNumber == 2) return "green";
    if (chanNumber == 3) return "blue";
    return "";
}

function findSplitChannelTitle(originalTitle, chanNumber) {
    expectedTitle = "C" + chanNumber + "-" + originalTitle;
    if (isOpen(expectedTitle)) return expectedTitle;

    stem = File.getNameWithoutExtension(originalTitle);
    color = channelColorName(chanNumber);
    if (lengthOf(color) > 0) {
        colorTitle = originalTitle + " (" + color + ")";
        if (isOpen(colorTitle)) return colorTitle;
        colorStemTitle = stem + " (" + color + ")";
        if (isOpen(colorStemTitle)) return colorStemTitle;
    }

    titles = getList("image.titles");
    prefix = "C" + chanNumber + "-";
    colorNeedle = "(" + color + ")";
    for (ii = 0; ii < titles.length; ii++) {
        title = titles[ii];
        if (title == originalTitle) continue;
        hasOriginalName = indexOf(title, originalTitle) >= 0 || indexOf(title, stem) >= 0;
        if (startsWith(title, prefix) && hasOriginalName) return title;
        if (lengthOf(color) > 0 && hasOriginalName && indexOf(toLowerCase(title), colorNeedle) >= 0) return title;
    }

    return "";
}

function currentImageTitleList() {
    titles = getList("image.titles");
    out = "";
    for (ii = 0; ii < titles.length; ii++) {
        out = out + "\n- " + titles[ii];
    }
    if (lengthOf(out) == 0) out = "\n- (none)";
    return out;
}

function closeSplitChannelWindows(originalTitle) {
    stem = File.getNameWithoutExtension(originalTitle);
    for (cc = 1; cc <= 8; cc++) {
        closeIfOpen("C" + cc + "-" + originalTitle);
        closeIfOpen("C" + cc + "-" + stem);
        color = channelColorName(cc);
        if (lengthOf(color) > 0) {
            closeIfOpen(originalTitle + " (" + color + ")");
            closeIfOpen(stem + " (" + color + ")");
        }
    }

    titles = getList("image.titles");
    for (ii = 0; ii < titles.length; ii++) {
        title = titles[ii];
        if (title == originalTitle) continue;
        hasOriginalName = indexOf(title, originalTitle) >= 0 || indexOf(title, stem) >= 0;
        if (!hasOriginalName) continue;
        lcTitle = toLowerCase(title);
        isSplitChannel = false;
        for (cc = 1; cc <= 8; cc++) {
            if (startsWith(title, "C" + cc + "-")) isSplitChannel = true;
        }
        if (indexOf(lcTitle, "(red)") >= 0) isSplitChannel = true;
        if (indexOf(lcTitle, "(green)") >= 0) isSplitChannel = true;
        if (indexOf(lcTitle, "(blue)") >= 0) isSplitChannel = true;
        if (isSplitChannel) {
            selectWindow(title);
            close();
        }
    }
}

function listImagesInDir(dir) {
    files = getFileList(dir);
    images = newArray(0);
    for (i = 0; i < files.length; i++) {
        f = toLowerCase(files[i]);
        if (endsWith(f, ".tif") || endsWith(f, ".tiff")
            || endsWith(f, ".png") || endsWith(f, ".jpg") || endsWith(f, ".jpeg")) {
            images = Array.concat(images, files[i]);
        }
    }
    return images;
}


// =============================================================================
// Minimal YAML helpers
//
// We don't try to implement a full YAML parser. Instead, two leaf-level
// operations on simple "section: { key: value, ... }" YAML are supported:
//
//   readYAML(yaml, section, key, default)
//   patchYAML(yaml, section, key, newValue)
//
// Use section = "" for top-level scalars (e.g., input_dir).
// =============================================================================

function indentLevel(line) {
    n = 0;
    while (n < lengthOf(line)) {
        c = substring(line, n, n + 1);
        if (c == " " || c == "\t") n++; else return n;
    }
    return n;
}

function stripComment(s) {
    // Strip an inline `# ...` comment, but keep YAML strings unaffected.
    // We assume our values aren't quoted strings containing '#'.
    idx = indexOf(s, "#");
    if (idx >= 0) return trim(substring(s, 0, idx));
    return s;
}

function readYAML(yamlText, section, key, defaultVal) {
    lines = split(yamlText, "\n");
    currentSection = "";
    for (i = 0; i < lines.length; i++) {
        line = lines[i];
        stripped = trim(line);
        if (lengthOf(stripped) == 0) continue;
        if (startsWith(stripped, "#")) continue;
        ind = indentLevel(line);
        colonIdx = indexOf(stripped, ":");
        if (colonIdx <= 0) continue;
        keyHere = substring(stripped, 0, colonIdx);
        rest = stripComment(trim(substring(stripped, colonIdx + 1)));
        if (ind == 0) {
            if (lengthOf(rest) == 0) {
                currentSection = keyHere;  // section header
            } else {
                currentSection = "";  // top-level scalar
                if (section == "" && keyHere == key) return rest;
            }
        } else {
            if (currentSection == section && keyHere == key) return rest;
        }
    }
    return defaultVal;
}

function patchYAML(yamlText, section, key, newValue) {
    lines = split(yamlText, "\n");
    currentSection = "";
    found = false;
    for (i = 0; i < lines.length; i++) {
        line = lines[i];
        stripped = trim(line);
        if (lengthOf(stripped) == 0) continue;
        if (startsWith(stripped, "#")) continue;
        ind = indentLevel(line);
        colonIdx = indexOf(stripped, ":");
        if (colonIdx <= 0) continue;
        keyHere = substring(stripped, 0, colonIdx);
        rest = trim(substring(stripped, colonIdx + 1));
        commentText = "";
        commentIdx = indexOf(rest, "#");
        if (commentIdx >= 0) commentText = "  " + substring(rest, commentIdx);
        if (ind == 0) {
            if (lengthOf(stripComment(rest)) == 0) {
                currentSection = keyHere;
            } else {
                currentSection = "";
                if (section == "" && keyHere == key) {
                    lines[i] = key + ": " + newValue + commentText;
                    found = true;
                }
            }
        } else {
            if (currentSection == section && keyHere == key) {
                indent = substring(line, 0, ind);
                lines[i] = indent + key + ": " + newValue + commentText;
                found = true;
            }
        }
    }
    out = "";
    for (i = 0; i < lines.length; i++) {
        if (i > 0) out = out + "\n";
        out = out + lines[i];
    }
    if (!found) {
        // If the (section, key) pair wasn't present, append it. This keeps
        // the macro robust against partial config files.
        if (section == "") {
            out = out + "\n" + key + ": " + newValue;
        } else {
            // Try to append under an existing section header, else create it.
            sectionHeader = "\n" + section + ":";
            sectIdx = indexOf("\n" + out, sectionHeader);
            if (sectIdx >= 0) {
                // Insert immediately after the header line.
                before = substring(out, 0, sectIdx + lengthOf(sectionHeader) - 1);
                after = substring(out, sectIdx + lengthOf(sectionHeader) - 1);
                out = before + "\n  " + key + ": " + newValue + after;
            } else {
                out = out + "\n" + section + ":\n  " + key + ": " + newValue;
            }
        }
    }
    return out;
}


// =============================================================================
// Command 0: Configure (Python path, pipeline script path)
// =============================================================================

function cmdConfigure() {
    py = readSetting("python_path", "python");
    script = readSetting("pipeline_script", "");
    if (py == "") py = "python";
    Dialog.create("Iba1Counter — Configure");
    Dialog.addMessage("Tell Iba1Counter where to find Python and the pipeline script.\n"
        + "Saved to ~/.iba1counter_prefs.txt and persists across Fiji restarts.");
    Dialog.addString("Python executable (or 'python' if on PATH):", py, 60);
    Dialog.addString("analyze_iba1_microglia.py FILE path:", script, 60);
    Dialog.addCheckbox("Browse for the script with a file picker", false);
    Dialog.addCheckbox("Test the Python executable now (runs `python --version`)", true);
    Dialog.show();
    py = Dialog.getString();
    script = Dialog.getString();
    browseScript = Dialog.getCheckbox();
    doTest = Dialog.getCheckbox();

    // File picker overrides the text field if requested.
    if (browseScript) {
        picked = File.openDialog("Select analyze_iba1_microglia.py");
        if (lengthOf(picked) > 0) script = picked;
    }

    if (lengthOf(py) == 0) { showMessage("Python path is empty."); return; }
    if (lengthOf(script) == 0) { showMessage("Pipeline script path is empty."); return; }

    // Auto-correct: if the user pointed at a directory, append the standard
    // filename. Common mistake when typing the path manually.
    if (File.exists(script) && File.isDirectory(script) == 1) {
        if (!endsWith(script, File.separator)) script = script + File.separator;
        candidate = script + "analyze_iba1_microglia.py";
        if (File.exists(candidate) && File.isDirectory(candidate) == 0) {
            print("Note: directory was given; auto-corrected script path to:");
            print("  " + candidate);
            script = candidate;
        } else {
            showMessage("Pipeline script not found",
                "The path is a directory. Looked for analyze_iba1_microglia.py inside\n"
                + "  " + script + "\n"
                + "but it wasn't there. Pick the script file directly (tick "
                + "'Browse for the script with a file picker').");
            return;
        }
    }

    // Final validation: must be an existing .py FILE.
    if (!File.exists(script)) {
        showMessage("Path not found", "Could not find file:\n" + script);
        return;
    }
    if (File.isDirectory(script) == 1) {
        showMessage("Not a file",
            "Script path is a directory, not a Python file:\n" + script);
        return;
    }
    if (!endsWith(toLowerCase(script), ".py")) {
        showMessage("Not a Python script",
            "Script path must end with .py:\n" + script);
        return;
    }

    if (doTest) {
        out = exec(py, "--version");
        print("\\Clear");
        print("python --version output:");
        print(out);
        if (lengthOf(trim(out)) == 0) {
            showMessage("Python test failed",
                "exec(\"" + py + "\", \"--version\") returned nothing.\n"
                + "Check the path or use a full path to python.exe.");
            return;
        }
    }
    writeSetting("python_path", py);
    writeSetting("pipeline_script", script);
    showMessage("Iba1Counter", "Saved.\nPython: " + py + "\nScript: " + script);
}


// =============================================================================
// Command 1: Setup Project
// =============================================================================

// Mode chooser: create a new project tree, or attach to one that already
// exists (e.g., reopened on a different machine, or one created earlier).
function cmdSetupProject() {
    Dialog.create("Project");
    modes = newArray("Create a new project", "Open an existing project");
    Dialog.addRadioButtonGroup("What would you like to do?", modes, 2, 1, modes[0]);
    Dialog.show();
    mode = Dialog.getRadioButton();
    if (startsWith(mode, "Open")) {
        cmdOpenExistingProject();
    } else {
        cmdCreateNewProject();
    }
}

// Open existing project: pick its folder, verify config.yaml exists, then
// register it as the active project. Missing input/rois/output subfolders
// are recreated so subsequent steps don't trip on layout drift.
function cmdOpenExistingProject() {
    dir = getDirectory("Choose existing project folder");
    if (lengthOf(dir) == 0) return;
    cfgPath = dir + "config.yaml";
    if (!File.exists(cfgPath)) {
        showMessage("Not an Iba1Counter project",
            "config.yaml was not found in:\n" + dir + "\n\n"
            + "Choose a folder that already contains a config.yaml, or use "
            + "'Create a new project' instead.");
        return;
    }
    ensureDir(dir + "input");
    ensureDir(dir + "rois");
    ensureDir(dir + "output");

    // Re-anchor absolute paths in case the project was moved/copied since
    // last use. Comments and other fields are preserved by patchYAML.
    yaml = File.openAsString(cfgPath);
    yaml = patchYAML(yaml, "",   "input_dir",  "\"" + fwdSlash(dir) + "input\"");
    yaml = patchYAML(yaml, "",   "output_dir", "\"" + fwdSlash(dir) + "output\"");
    yaml = patchYAML(yaml, "roi","directory",  "\"" + fwdSlash(dir) + "rois\"");
    File.saveString(yaml, cfgPath);

    writeSetting("project_dir",dir);

    print("\\Clear");
    print("Project loaded: " + dir);
    print("  - Input  : " + dir + "input");
    print("  - ROIs   : " + dir + "rois");
    print("  - Output : " + dir + "output");
    print("  - Config : " + cfgPath);
    showMessage("Iba1Counter", "Loaded existing project:\n" + dir);
}

function cmdCreateNewProject() {
    parent = getDirectory("Choose parent folder for new project");
    if (lengthOf(parent) == 0) return;
    Dialog.create("New Iba1Counter project");
    Dialog.addString("Project folder name:", "iba1_exp_001", 30);
    Dialog.show();
    name = Dialog.getString();
    if (lengthOf(trim(name)) == 0) { showMessage("Empty project name."); return; }
    projectDir = parent + name + File.separator;

    if (File.exists(projectDir)) {
        if (!getBoolean("Folder already exists. Use it anyway?")) return;
    } else {
        ensureDir(projectDir);
    }
    ensureDir(projectDir + "input");
    ensureDir(projectDir + "rois");
    ensureDir(projectDir + "output");

    cfgPath = projectDir + "config.yaml";
    if (!File.exists(cfgPath)) {
        // Copy from config_example.yaml next to the pipeline script if available.
        script = readSetting("pipeline_script", "");
        if (lengthOf(script) > 0) {
            scriptDir = File.getParent(script);
            example = scriptDir + File.separator + "config_example.yaml";
            if (File.exists(example)) {
                File.saveString(File.openAsString(example), cfgPath);
            }
        }
        if (!File.exists(cfgPath)) {
            // Last resort: write a minimal default config.
            File.saveString(defaultMinimalConfig(), cfgPath);
        }
    }

    // Patch absolute paths so the config works from any cwd.
    yaml = File.openAsString(cfgPath);
    yaml = patchYAML(yaml, "", "input_dir", "\"" + fwdSlash(projectDir) + "input\"");
    yaml = patchYAML(yaml, "", "output_dir", "\"" + fwdSlash(projectDir) + "output\"");
    yaml = patchYAML(yaml, "roi", "directory", "\"" + fwdSlash(projectDir) + "rois\"");
    yaml = patchYAML(yaml, "roi", "fallback_whole_image", "false");
    File.saveString(yaml, cfgPath);

    writeSetting("project_dir",projectDir);

    print("\\Clear");
    print("Project ready: " + projectDir);
    print("  - Drop TIFFs in: " + projectDir + "input");
    print("  - ROIs will be saved to: " + projectDir + "rois");
    print("  - Outputs go to: " + projectDir + "output");
    print("  - Config: " + cfgPath);
    showMessage("Iba1Counter",
        "Project ready.\n\nDrop your TIFF images in:\n" + projectDir + "input\n\n"
        + "Then re-open this menu and run:\n  3. Draw ROIs\n  4. Run analysis");
}


// =============================================================================
// Command 2: Edit parameters (subset of config.yaml as Fiji dialog)
// =============================================================================

function cmdEditParameters() {
    proj = readSetting("project_dir", "");
    if (lengthOf(proj) == 0) {
        showMessage("No active project. Run Setup Project first.");
        return;
    }
    cfgPath = proj + "config.yaml";
    if (!File.exists(cfgPath)) {
        showMessage("config.yaml not found in project folder.");
        return;
    }
    yaml = File.openAsString(cfgPath);

    px         = readYAML(yaml, "pixel_size",       "um_per_px",            "null");
    chanMode   = readYAML(yaml, "channel",          "mode",                 "rgb");
    chanIdx    = readYAML(yaml, "channel",          "index",                "1");
    bgRadius   = readYAML(yaml, "background",       "radius_px",            "50.0");
    somaR      = readYAML(yaml, "soma_enhancement", "soma_radius_px",       "6.0");
    seedThr    = readYAML(yaml, "seed_detection",   "min_peak_intensity",   "50.0");
    seedDist   = readYAML(yaml, "seed_detection",   "min_distance_px",      "8.0");
    segThr     = readYAML(yaml, "segmentation",     "soma_mask_intensity",  "30.0");
    minArea    = readYAML(yaml, "object_filter",    "min_area_um2",         "15.0");
    maxArea    = readYAML(yaml, "object_filter",    "max_area_um2",         "200.0");
    objMean    = readYAML(yaml, "object_filter",    "min_mean_intensity",   "30.0");
    objPeak    = readYAML(yaml, "object_filter",    "min_peak_intensity",   "60.0");
    paramId    = readYAML(yaml, "",                 "parameter_set_id",     "default_v1");

    Dialog.create("Iba1Counter — parameters");
    Dialog.addMessage("These overwrite the matching fields in config.yaml.\n"
        + "Other fields and comments in the YAML are preserved.");
    Dialog.addString("parameter_set_id:", paramId, 24);
    Dialog.addMessage("--- Pixel calibration ---");
    Dialog.addString("pixel_size.um_per_px (number, or 'null' to read from TIFF):", px, 12);
    Dialog.addMessage("--- Channel ---");
    Dialog.addChoice("channel.mode:", newArray("rgb", "multi", "single", "auto"), chanMode);
    Dialog.addNumber("channel.index (0=R, 1=G, 2=B for RGB):", parseFloat(chanIdx));
    Dialog.addMessage("--- Background ---");
    Dialog.addNumber("background.radius_px (>= 5x soma_radius_px):", parseFloat(bgRadius));
    Dialog.addMessage("--- Soma enhancement (most influential) ---");
    Dialog.addNumber("soma_enhancement.soma_radius_px (px):", parseFloat(somaR));
    Dialog.addMessage("--- Seed detection (FIXED absolute thresholds) ---");
    Dialog.addNumber("seed_detection.min_peak_intensity:", parseFloat(seedThr));
    Dialog.addNumber("seed_detection.min_distance_px:", parseFloat(seedDist));
    Dialog.addMessage("--- Segmentation ---");
    Dialog.addNumber("segmentation.soma_mask_intensity:", parseFloat(segThr));
    Dialog.addMessage("--- Object filters (primary: area + intensity) ---");
    Dialog.addNumber("object_filter.min_area_um2:", parseFloat(minArea));
    Dialog.addNumber("object_filter.max_area_um2:", parseFloat(maxArea));
    Dialog.addNumber("object_filter.min_mean_intensity:", parseFloat(objMean));
    Dialog.addNumber("object_filter.min_peak_intensity:", parseFloat(objPeak));
    Dialog.show();

    paramId    = Dialog.getString();
    px         = Dialog.getString();
    chanMode   = Dialog.getChoice();
    chanIdx    = "" + Dialog.getNumber();
    bgRadius   = "" + Dialog.getNumber();
    somaR      = "" + Dialog.getNumber();
    seedThr    = "" + Dialog.getNumber();
    seedDist   = "" + Dialog.getNumber();
    segThr     = "" + Dialog.getNumber();
    minArea    = "" + Dialog.getNumber();
    maxArea    = "" + Dialog.getNumber();
    objMean    = "" + Dialog.getNumber();
    objPeak    = "" + Dialog.getNumber();

    yaml = patchYAML(yaml, "",                 "parameter_set_id",     paramId);
    yaml = patchYAML(yaml, "pixel_size",       "um_per_px",            px);
    yaml = patchYAML(yaml, "channel",          "mode",                 chanMode);
    yaml = patchYAML(yaml, "channel",          "index",                chanIdx);
    yaml = patchYAML(yaml, "background",       "radius_px",            bgRadius);
    yaml = patchYAML(yaml, "soma_enhancement", "soma_radius_px",       somaR);
    yaml = patchYAML(yaml, "seed_detection",   "min_peak_intensity",   seedThr);
    yaml = patchYAML(yaml, "seed_detection",   "min_distance_px",      seedDist);
    yaml = patchYAML(yaml, "segmentation",     "soma_mask_intensity",  segThr);
    yaml = patchYAML(yaml, "object_filter",    "min_area_um2",         minArea);
    yaml = patchYAML(yaml, "object_filter",    "max_area_um2",         maxArea);
    yaml = patchYAML(yaml, "object_filter",    "min_mean_intensity",   objMean);
    yaml = patchYAML(yaml, "object_filter",    "min_peak_intensity",   objPeak);
    File.saveString(yaml, cfgPath);

    print("\\Clear");
    print("Updated " + cfgPath);
}


// =============================================================================
// Command 3: Draw ROIs interactively
// =============================================================================

function cmdDrawROIs() {
    proj = readSetting("project_dir", "");
    if (lengthOf(proj) == 0) { showMessage("No active project. Run Setup Project first."); return; }
    inputDir = proj + "input" + File.separator;
    roiDir   = proj + "rois"  + File.separator;
    if (!File.exists(inputDir)) { showMessage("input/ folder missing."); return; }
    ensureDir(roiDir);

    images = listImagesInDir(inputDir);
    if (images.length == 0) {
        showMessage("No images found in " + inputDir);
        return;
    }

    Dialog.create("Draw ROIs");
    Dialog.addMessage(images.length + " image(s) found in input/.");
    Dialog.addCheckbox("Skip images that already have <stem>.zip in rois/", true);
    Dialog.addCheckbox("Allow multiple ROIs per image (press T after each)", true);
    Dialog.addChoice("Default ROI tool:", newArray("polygon", "freehand", "rectangle", "ellipse"), "polygon");
    Dialog.show();
    skipDone = Dialog.getCheckbox();
    multiROI = Dialog.getCheckbox();
    toolName = Dialog.getChoice();

    setTool(toolName);

    nProcessed = 0;
    for (i = 0; i < images.length; i++) {
        name = images[i];
        stem = File.getNameWithoutExtension(name);
        roiZip = roiDir + stem + ".zip";
        if (skipDone && File.exists(roiZip)) continue;

        path = inputDir + name;
        open(path);
        roiManager("reset");
        run("Select None");
        setTool(toolName);

        if (multiROI) {
            msg = "Image " + (i + 1) + " of " + images.length + ":\n" + name + "\n\n"
                + "Draw an ROI, then press T (or click 'Add' in ROI Manager).\n"
                + "Repeat for additional ROIs.\n"
                + "Click OK when all ROIs for this image are added.";
        } else {
            msg = "Image " + (i + 1) + " of " + images.length + ":\n" + name + "\n\n"
                + "Draw a single ROI, then click OK.\n"
                + "Click Cancel to skip this image.";
        }
        waitForUser("Draw ROI", msg);

        nROI = roiManager("count");
        if (nROI == 0) {
            // If the user has an active selection but didn't press T, add it.
            if (selectionType() >= 0) {
                roiManager("Add");
                nROI = 1;
            }
        }
        if (nROI == 0) {
            print("Skipped " + name + " (no ROI added).");
            close();
            continue;
        }
        if (File.exists(roiZip)) File.delete(roiZip);
        roiManager("Save", roiZip);
        nProcessed++;
        roiNoun = " ROI";
        if (nROI > 1) roiNoun = " ROIs";
        print("Saved " + roiZip + " (" + nROI + roiNoun + ")");
        close();
    }
    showMessage("Iba1Counter", "Processed " + nProcessed + " image(s).\nROIs saved to:\n" + roiDir);
}


// =============================================================================
// Command 4: Run analysis (calls Python)
// =============================================================================

function cmdRunAnalysis() {
    proj = readSetting("project_dir", "");
    if (lengthOf(proj) == 0) { showMessage("No active project. Run Setup Project first."); return; }
    baseCfgPath = proj + "config.yaml";
    fijiCfgPath = proj + "config_fiji_bg.yaml";
    cfgPath = baseCfgPath;
    if (!File.exists(baseCfgPath)) { showMessage("config.yaml not found."); return; }
    py = readSetting("python_path", "python");
    script = readSetting("pipeline_script", "");
    if (lengthOf(py) == 0 || lengthOf(script) == 0 || !File.exists(script)) {
        showMessage("Python or pipeline script is not configured. Tick 'Configure paths' on the main menu first.");
        return;
    }
    if (File.isDirectory(script) == 1) {
        showMessage("Pipeline script is a directory",
            "The configured script path is a directory, not a .py file:\n"
            + script + "\n\n"
            + "Re-open Configure paths and select analyze_iba1_microglia.py specifically.");
        return;
    }

    baseYaml = File.openAsString(baseCfgPath);
    bgRadiusDefault = readYAML(baseYaml, "background", "radius_px", "50.0");
    baseOutputDir = stripOuterQuotes(readYAML(baseYaml, "", "output_dir", proj + "output"));
    baseOutputDir = ensureTrailingSeparator(baseOutputDir);

    // Extra options. --validate / --optimize have config prerequisites that
    // the Python pipeline enforces; pre-flight here so the user gets a clear
    // error rather than waiting for exec to fail.
    Dialog.create("Run analysis");
    Dialog.addMessage("Default: Fiji extracts the Iba1 channel, runs Subtract Background, then Python continues.");
    Dialog.addCheckbox("Use Fiji Subtract Background before Python", true);
    Dialog.addNumber("Fiji rolling ball radius (px):", parseFloat(bgRadiusDefault));
    Dialog.addCheckbox("Use sliding paraboloid", false);
    Dialog.addCheckbox("Light background", false);
    Dialog.addCheckbox("Disable smoothing", false);
    Dialog.addCheckbox("Overwrite existing Fiji-corrected images", true);
    Dialog.addMessage("Run the standard detection workflow and write CSV/QC outputs.");
    Dialog.addCheckbox("Open image_summary.csv after the run", true);
    Dialog.addCheckbox("Open QC overlays after the run", false);
    Dialog.show();
    useFijiBg = Dialog.getCheckbox();
    fijiRadius = Dialog.getNumber();
    useSliding = Dialog.getCheckbox();
    useLight = Dialog.getCheckbox();
    disableSmoothing = Dialog.getCheckbox();
    overwriteFijiBg = Dialog.getCheckbox();
    doValidate = false;
    doOptimize = false;
    openSummary = Dialog.getCheckbox();
    openQC = Dialog.getCheckbox();

    if (useFijiBg) {
        cfgPath = createFijiBackgroundConfig(
            fijiRadius, useSliding, useLight, disableSmoothing, overwriteFijiBg, baseOutputDir
        );
        if (lengthOf(cfgPath) == 0) return;
    }

    cfgYamlForOutput = File.openAsString(cfgPath);
    outputDir = stripOuterQuotes(readYAML(cfgYamlForOutput, "", "output_dir", proj + "output"));
    outputDir = ensureTrailingSeparator(outputDir);

    extraFlag = "";
    if (doOptimize) {
        cfgYaml = File.openAsString(cfgPath);
        optEnabled = readYAML(cfgYaml, "optimization", "enabled", "false");
        optCsv     = readYAML(cfgYaml, "optimization", "manual_counts_csv", "null");
        if (optEnabled != "true") {
            showMessage("Optimization not enabled in config",
                "You ticked 'Optimize parameters' but config.yaml has\n"
                + "  optimization.enabled: " + optEnabled + "\n\n"
                + "To use --optimize you need ALL of:\n"
                + "  optimization.enabled: true\n"
                + "  optimization.manual_counts_csv: \"<path to manual_counts.csv>\"\n"
                + "  optimization.grids: { ... parameter ranges ... }\n\n"
                + "See docs/parameter_tuning.md for guidance, or just run a normal "
                + "analysis (uncheck both boxes) to start.");
            return;
        }
        if (optCsv == "null" || lengthOf(trim(optCsv)) == 0) {
            showMessage("Manual counts CSV missing",
                "optimization.enabled is true but manual_counts_csv is not set in config.yaml.");
            return;
        }
        extraFlag = " --optimize";
    } else if (doValidate) {
        cfgYaml = File.openAsString(cfgPath);
        valEnabled = readYAML(cfgYaml, "validation", "enabled", "false");
        valCsv     = readYAML(cfgYaml, "validation", "manual_counts_csv", "null");
        if (valEnabled != "true") {
            showMessage("Validation not enabled in config",
                "You ticked 'Validate against manual counts' but config.yaml has\n"
                + "  validation.enabled: " + valEnabled + "\n\n"
                + "To use --validate you need:\n"
                + "  validation.enabled: true\n"
                + "  validation.manual_counts_csv: \"<path to manual_counts.csv>\"\n\n"
                + "Or just run a normal analysis (uncheck both boxes) to start.");
            return;
        }
        if (valCsv == "null" || lengthOf(trim(valCsv)) == 0) {
            showMessage("Manual counts CSV missing",
                "validation.enabled is true but manual_counts_csv is not set in config.yaml.");
            return;
        }
        extraFlag = " --validate";
    }

    // Capture stderr too. IJM's exec() reads only stdout, so any Python
    // import error / argparse error / unhandled exception that prints to
    // stderr is otherwise invisible. Wrap the call in cmd /c with 2>&1.
    // The "" outer-quotes wrap the whole command per cmd's quoting rules,
    // letting paths-with-spaces work without ambiguity.
    shellCmd = "\"\"" + py + "\" \"" + script + "\" --config \"" + cfgPath + "\""
        + extraFlag + " 2>&1\"";

    print("\\Clear");
    print("=== Iba1Counter pipeline ===");
    print("Python : " + py);
    print("Script : " + script);
    print("Config : " + cfgPath);
    print("Output : " + outputDir);
    print("Project: " + proj);
    print("Started: " + getTimeStamp());
    print("Command: cmd /c " + shellCmd);
    print("");
    showStatus("Iba1Counter: running pipeline...");
    setBatchMode(true);  // suppress UI redraws while we wait

    out = exec("cmd", "/c", shellCmd);

    setBatchMode(false);
    print("--- exec output ---");
    if (lengthOf(trim(out)) == 0) {
        print("[exec returned empty -- the subprocess produced no output on either stream]");
    } else {
        print(out);
    }
    print("--- end exec output ---");
    print("");

    // Always echo the pipeline's run.log if it exists. The pipeline writes
    // it from setup_logger; even if exec output is empty, run.log may still
    // contain the partial log up to the point of failure.
    runLog = outputDir + "run.log";
    if (File.exists(runLog)) {
        logText = File.openAsString(runLog);
        if (lengthOf(trim(logText)) > 0) {
            print("--- run.log ---");
            print(logText);
            print("--- end run.log ---");
            print("");
        }
    }

    print("=== Done at " + getTimeStamp() + " ===");
    showStatus("Iba1Counter: done");

    summary = outputDir + "image_summary.csv";
    if (!File.exists(summary)) {
        diag = "image_summary.csv was not produced.\n\n";
        if (lengthOf(trim(out)) == 0) {
            diag = diag + "exec() returned no output and no run.log was written.\n"
                + "Python likely crashed before logging started. Common causes:\n"
                + "  - Required Python packages not installed\n"
                + "    (run: pip install -r requirements.txt)\n"
                + "  - Wrong python.exe selected in Configure\n"
                + "  - config.yaml has invalid paths or YAML syntax errors\n\n"
                + "To see the underlying error, open Command Prompt and run:\n"
                + "  \"" + py + "\" \"" + script + "\" --config \"" + cfgPath + "\"";
        } else {
            diag = diag + "Check the Log window above for the Python error output.";
        }
        showMessage("Iba1Counter", diag);
        return;
    }

    // Header-only CSV = every image failed. The pipeline still writes the
    // file, so File.exists is not enough; count the data rows.
    summaryText = File.openAsString(summary);
    summaryLines = split(summaryText, "\n");
    nDataRows = 0;
    for (i = 1; i < summaryLines.length; i++) {  // skip header
        if (lengthOf(trim(summaryLines[i])) > 0) nDataRows++;
    }
    if (nDataRows == 0) {
        showMessage("Iba1Counter — no images processed",
            "image_summary.csv has 0 data rows: every image in input/ failed.\n\n"
            + "Most likely cause: required Python package missing for your TIFFs.\n"
            + "If your TIFFs are LZW-compressed, install imagecodecs:\n"
            + "  pip install imagecodecs\n\n"
            + "See the run.log section in the Log window above for the exact\n"
            + "exception message per image.");
        return;
    }

    if (openSummary) open(summary);
    if (openQC) cmdReviewQC();
}


// =============================================================================
// Helper: create Fiji Subtract Background input for the Python pipeline
// =============================================================================

function createFijiBackgroundConfig(radius, useSliding, useLight, disableSmoothing, overwrite, outputDir) {
    proj = readSetting("project_dir", "");
    if (lengthOf(proj) == 0) { showMessage("No active project. Run Setup Project first."); return ""; }
    inputDir = proj + "input" + File.separator;
    bgDir = proj + "input_fiji_bg" + File.separator;
    outputDir = ensureTrailingSeparator(outputDir);
    cfgPath = proj + "config.yaml";
    if (!File.exists(inputDir)) { showMessage("input/ folder missing."); return ""; }
    if (!File.exists(cfgPath)) { showMessage("config.yaml not found."); return ""; }

    images = listImagesInDir(inputDir);
    if (images.length == 0) {
        showMessage("No images found in " + inputDir);
        return "";
    }

    yaml = File.openAsString(cfgPath);
    paramId = readYAML(yaml, "", "parameter_set_id", "default_v1");
    chanIdx = readYAML(yaml, "channel", "index", "1");
    chanNumber = parseInt(chanIdx) + 1;  // ImageJ Split Channels uses C1/C2/C3
    if (chanNumber < 1) chanNumber = 2;

    ensureDir(bgDir);
    ensureDir(outputDir);

    opts = "rolling=" + radius;
    if (useSliding) opts = opts + " sliding";
    if (useLight) opts = opts + " light";
    if (disableSmoothing) opts = opts + " disable";

    print("\\Clear");
    print("=== Fiji Subtract Background input generation ===");
    print("Input : " + inputDir);
    print("Output: " + bgDir);
    print("Channel: C" + chanNumber + " (channel.index=" + chanIdx + ")");
    print("Options: " + opts);
    print("");

    setBatchMode(true);
    nWritten = 0;
    nSkipped = 0;
    for (i = 0; i < images.length; i++) {
        name = images[i];
        stem = File.getNameWithoutExtension(name);
        src = inputDir + name;
        dst = bgDir + stem + ".tif";
        if (!overwrite && File.exists(dst)) {
            nSkipped++;
            continue;
        }
        open(src);
        originalTitle = getTitle();
        run("Split Channels");
        channelTitle = findSplitChannelTitle(originalTitle, chanNumber);
        if (lengthOf(channelTitle) == 0) {
            setBatchMode(false);
            showMessage("Channel not found",
                "Could not find split channel for C" + chanNumber + " from:\n"
                + originalTitle + "\n\n"
                + "Open image windows after Split Channels:" + currentImageTitleList() + "\n\n"
                + "Check channel.index in config.yaml.");
            return "";
        }
        selectWindow(channelTitle);
        run("Subtract Background...", opts);
        saveAs("Tiff", dst);
        close();
        closeSplitChannelWindows(originalTitle);
        closeIfOpen(originalTitle);
        nWritten++;
        print("Wrote " + dst);
    }
    setBatchMode(false);

    fijiCfgPath = proj + "config_fiji_bg.yaml";
    fijiYaml = File.openAsString(cfgPath);
    fijiYaml = patchYAML(fijiYaml, "", "output_dir", "\"" + fwdSlash(outputDir) + "\"");
    fijiYaml = patchYAML(fijiYaml, "", "parameter_set_id", paramId + "_fiji_bg");
    fijiYaml = patchYAML(fijiYaml, "background", "method", "external");
    fijiYaml = patchYAML(fijiYaml, "background", "radius_px", "" + radius);
    fijiYaml = patchYAML(fijiYaml, "background", "external_dir", "\"" + fwdSlash(bgDir) + "\"");
    File.saveString(fijiYaml, fijiCfgPath);

    print("");
    print("Wrote Fiji-background config: " + fijiCfgPath);
    print("Images written: " + nWritten + ", skipped: " + nSkipped);

    return fijiCfgPath;
}


// =============================================================================
// Command 5: Review QC overlays
// =============================================================================

function cmdReviewQC() {
    proj = readSetting("project_dir", "");
    if (lengthOf(proj) == 0) { showMessage("No active project."); return; }
    outputDir = proj + "output" + File.separator;
    fijiOutputDir = proj + "output_fiji_bg" + File.separator;
    if (File.exists(fijiOutputDir + "qc_overlays" + File.separator)) {
        Dialog.create("QC overlays");
        Dialog.addChoice("Output folder:",
            newArray("output", "output_fiji_bg"),
            "output");
        Dialog.show();
        pick = Dialog.getChoice();
        if (pick == "output_fiji_bg") outputDir = fijiOutputDir;
    }
    qcDir = outputDir + "qc_overlays" + File.separator;
    if (!File.exists(qcDir)) { showMessage("QC overlays folder not found. Run analysis first."); return; }
    files = getFileList(qcDir);
    if (files.length == 0) { showMessage("No QC overlays in " + qcDir); return; }
    // Open each PNG/TIF as a separate window. For a stack view, the user can use
    // File > Import > Image Sequence on the qcDir.
    nOpen = 0;
    for (i = 0; i < files.length; i++) {
        f = files[i];
        lc = toLowerCase(f);
        if (endsWith(lc, ".png") || endsWith(lc, ".tif") || endsWith(lc, ".tiff")) {
            open(qcDir + f);
            nOpen++;
            if (nOpen >= 16) {
                showMessage("Opened first 16 overlays. The rest are in:\n" + qcDir);
                return;
            }
        }
    }
    if (nOpen == 0) showMessage("No PNG/TIFF overlays found.");
}


// =============================================================================
// Command 6: Apply manual corrections
// =============================================================================

function cmdApplyCorrections() {
    proj = readSetting("project_dir", "");
    if (lengthOf(proj) == 0) { showMessage("No active project."); return; }
    inputDir = proj + "input" + File.separator;
    perObj   = proj + "output" + File.separator + "per_object.csv";
    cfgPath  = proj + "config.yaml";
    if (!File.exists(perObj)) { showMessage("per_object.csv not found. Run analysis first."); return; }

    Dialog.create("Manual corrections");
    Dialog.addMessage(
        "For each image you choose to correct, the macro will:\n"
        + "  1. Open the original image\n"
        + "  2. Draw the algorithm's ACCEPTED detections as cyan circles\n"
        + "  3. Ask you to mark MISSED cells with the Multi-point tool\n"
        + "  4. Ask you to mark FALSE POSITIVES the same way\n"
        + "All marks are written to corrections.csv.");
    Dialog.addString("Reviewer initials:", "JD", 8);
    Dialog.addCheckbox("Blinded review", true);
    Dialog.addCheckbox("Process every image (uncheck to pick one)", true);
    Dialog.addNumber("Circle radius for accepted detections (px):", 6);
    Dialog.show();
    reviewer = Dialog.getString();
    blinded = Dialog.getCheckbox();
    processAll = Dialog.getCheckbox();
    drawR = Dialog.getNumber();

    // Build the list of images to process
    images = listImagesInDir(inputDir);
    if (!processAll) {
        if (images.length == 0) { showMessage("No input images."); return; }
        Dialog.create("Pick image");
        Dialog.addChoice("Image:", images, images[0]);
        Dialog.show();
        only = Dialog.getChoice();
        images = newArray(only);
    }

    // Locate column indices in per_object.csv
    perObjText = File.openAsString(perObj);
    rows = split(perObjText, "\n");
    if (rows.length < 2) { showMessage("per_object.csv has no data rows."); return; }
    header = rows[0];
    cols = split(header, ",");
    iImg = -1; iROI = -1; iX = -1; iY = -1; iStatus = -1;
    for (j = 0; j < cols.length; j++) {
        h = trim(cols[j]);
        if (h == "image_id") iImg = j;
        if (h == "roi_id") iROI = j;
        if (h == "x_centroid_px") iX = j;
        if (h == "y_centroid_px") iY = j;
        if (h == "accepted_or_rejected") iStatus = j;
    }
    if (iImg < 0 || iROI < 0 || iX < 0 || iY < 0 || iStatus < 0) {
        showMessage("per_object.csv is missing expected columns.");
        return;
    }

    // Output CSV
    correctionsPath = proj + "corrections.csv";
    correctionsHeader = "image_id,roi_id,action,x,y,reason,reviewer,blinded_condition";
    if (File.exists(correctionsPath)) {
        if (getBoolean("corrections.csv already exists. Overwrite?")) {
            File.saveString(correctionsHeader + "\n", correctionsPath);
        }
        // else: append
    } else {
        File.saveString(correctionsHeader + "\n", correctionsPath);
    }

    blindedStr = "no";
    if (blinded) blindedStr = "yes";

    nAddTotal = 0;
    nRemTotal = 0;

    for (i = 0; i < images.length; i++) {
        name = images[i];
        stem = File.getNameWithoutExtension(name);
        path = inputDir + name;
        if (!File.exists(path)) continue;
        open(path);

        // Find all distinct roi_ids for this image_id in per_object.csv
        roiSet = newArray(0);
        for (k = 1; k < rows.length; k++) {
            line = rows[k];
            if (lengthOf(trim(line)) == 0) continue;
            r = split(line, ",");
            if (r.length <= iStatus) continue;
            if (trim(r[iImg]) != stem) continue;
            rid = trim(r[iROI]);
            seen = false;
            for (m = 0; m < roiSet.length; m++) {
                if (roiSet[m] == rid) { seen = true; break; }
            }
            if (!seen) roiSet = Array.concat(roiSet, rid);
        }
        if (roiSet.length == 0) {
            print("No detections in per_object.csv for " + stem + " — skipping.");
            close();
            continue;
        }

        for (rIdx = 0; rIdx < roiSet.length; rIdx++) {
            rid = roiSet[rIdx];

            // Draw accepted detections as small cyan circles in ROI Manager
            roiManager("reset");
            for (k = 1; k < rows.length; k++) {
                line = rows[k];
                if (lengthOf(trim(line)) == 0) continue;
                r = split(line, ",");
                if (r.length <= iStatus) continue;
                if (trim(r[iImg]) != stem) continue;
                if (trim(r[iROI]) != rid) continue;
                if (trim(r[iStatus]) != "accepted") continue;
                xv = parseFloat(r[iX]);
                yv = parseFloat(r[iY]);
                makeOval(xv - drawR, yv - drawR, 2 * drawR, 2 * drawR);
                roiManager("Add");
            }
            roiManager("Show All");
            run("Enhance Contrast", "saturated=0.35");
            setTool("multipoint");

            // ROUND 1: ADD missed cells
            run("Select None");
            waitForUser(
                "MISSED cells (image " + (i + 1) + "/" + images.length + ", ROI=" + rid + ")",
                "Cyan circles = algorithm's accepted detections.\n\n"
                + "Use the Multi-point tool to mark cells the algorithm MISSED.\n"
                + "Click each missed cell, then click OK.\n"
                + "(If none, just click OK.)"
            );
            xs1 = newArray(0);
            ys1 = newArray(0);
            if (selectionType() == 10) getSelectionCoordinates(xs1, ys1);

            // ROUND 2: REMOVE false positives
            run("Select None");
            waitForUser(
                "FALSE POSITIVES (image " + (i + 1) + "/" + images.length + ", ROI=" + rid + ")",
                "Use the Multi-point tool to mark detections that should be REMOVED\n"
                + "(false positives — cyan circles that aren't real cells).\n"
                + "Click each false positive, then click OK."
            );
            xs2 = newArray(0);
            ys2 = newArray(0);
            if (selectionType() == 10) getSelectionCoordinates(xs2, ys2);

            // Append rows to corrections.csv
            buf = "";
            for (k = 0; k < xs1.length; k++) {
                buf = buf + stem + "," + rid + ",add," + xs1[k] + "," + ys1[k]
                    + ",missed_cell," + reviewer + "," + blindedStr + "\n";
            }
            for (k = 0; k < xs2.length; k++) {
                buf = buf + stem + "," + rid + ",remove," + xs2[k] + "," + ys2[k]
                    + ",false_positive," + reviewer + "," + blindedStr + "\n";
            }
            if (lengthOf(buf) > 0) {
                File.append(buf, correctionsPath);
            }
            nAddTotal = nAddTotal + xs1.length;
            nRemTotal = nRemTotal + xs2.length;
            print(stem + " / " + rid + ": +" + xs1.length + " adds, -" + xs2.length + " removes");
        }
        close();
    }

    // Wire corrections into config and offer to re-run
    yaml = File.openAsString(cfgPath);
    yaml = patchYAML(yaml, "corrections", "enabled", "true");
    yaml = patchYAML(yaml, "corrections", "corrections_csv", "\"" + fwdSlash(correctionsPath) + "\"");
    File.saveString(yaml, cfgPath);

    showMessage("Manual corrections",
        "Recorded " + nAddTotal + " additions and " + nRemTotal + " removals.\n"
        + "Saved to: " + correctionsPath + "\n\n"
        + "config.yaml has been updated to enable corrections.\n"
        + "Re-run the analysis to compute corrected counts.");

    if (getBoolean("Re-run analysis now with corrections enabled?")) cmdRunAnalysis();
}


// =============================================================================
// Helpers used by commands
// =============================================================================

function getTimeStamp() {
    getDateAndTime(year, month, dow, dom, hr, min, sec, ms);
    return "" + year + "-" + pad2(month + 1) + "-" + pad2(dom)
        + " " + pad2(hr) + ":" + pad2(min) + ":" + pad2(sec);
}

function pad2(n) {
    if (n < 10) return "0" + n;
    return "" + n;
}

function defaultMinimalConfig() {
    s = "";
    s = s + "input_dir: input\n";
    s = s + "output_dir: output\n";
    s = s + "file_glob: \"*.tif*\"\n";
    s = s + "parameter_set_id: default_v1\n";
    s = s + "save_rejected_objects: true\n";
    s = s + "channel:\n  mode: rgb\n  index: 1\n";
    s = s + "pixel_size:\n  um_per_px: null\n  require_metadata: false\n";
    s = s + "roi:\n  directory: rois\n  suffix: \".zip\"\n  fallback_whole_image: false\n";
    s = s + "background:\n  method: rolling_ball\n  radius_px: 50.0\n  external_dir: null\n";
    s = s + "denoising:\n  method: median\n  median_size_px: 3\n  gaussian_sigma_px: 0.8\n";
    s = s + "soma_enhancement:\n  method: tophat_dog\n  soma_radius_px: 6.0\n  dog_sigma_ratio: 1.6\n";
    s = s + "seed_detection:\n  min_distance_px: 8.0\n  min_peak_intensity: 50.0\n  exclude_border_px: 2\n";
    s = s + "segmentation:\n  soma_mask_intensity: 30.0\n  enhanced_mask_fraction: 0.25\n  max_soma_radius_factor: 2.5\n";
    s = s + "object_filter:\n  min_area_um2: 15.0\n  max_area_um2: 200.0\n  min_mean_intensity: 30.0\n  min_peak_intensity: 60.0\n  exclude_edge_objects: true\n  edge_margin_px: 1\n";
    s = s + "intensity:\n  area_fraction_threshold: 30.0\n  use_otsu_for_area_fraction: false\n";
    s = s + "qc:\n  save_overlays: true\n  show_rejected: true\n  figure_dpi: 150\n  overlay_format: png\n";
    s = s + "validation:\n  enabled: false\n  manual_counts_csv: null\n  output_subdir: validation\n";
    s = s + "optimization:\n  enabled: false\n  manual_counts_csv: null\n  metric: mae_balanced\n  grids: {}\n";
    s = s + "corrections:\n  enabled: false\n  corrections_csv: null\n  radius_for_remove_px: 6.0\n";
    return s;
}


// =============================================================================
// Main dispatcher
// =============================================================================

// The menu loops until the user clicks the Dialog's Cancel button (which
// raises an IJM abort exception and stops the macro cleanly). After each
// completed action it returns to this dialog so the user does not have to
// re-launch the macro between steps.
function main() {
    options = newArray(
        "1. Set up new project folder",
        "2. Edit analysis parameters",
        "3. Draw ROIs interactively",
        "4. Run analysis (Python)",
        "5. Open QC overlays",
        "6. Apply manual corrections"
    );

    while (true) {
        proj = readSetting("project_dir", "");
        py = readSetting("python_path", "python");
        script = readSetting("pipeline_script", "");

        projDisp = "(none)";
        if (lengthOf(proj) > 0) projDisp = proj;
        pyDisp = "(not set)";
        if (lengthOf(py) > 0) pyDisp = py;
        scriptDisp = "(not set)";
        if (lengthOf(script) > 0) scriptDisp = script;

        Dialog.create("Iba1Counter");
        Dialog.addMessage("Project : " + projDisp);
        Dialog.addMessage("Python  : " + pyDisp);
        Dialog.addMessage("Script  : " + scriptDisp);
        // Configure "button" — Dialog has no native button widget, so we
        // place a checkbox on the same row as the Script line. Ticking it
        // opens the Configure dialog INSTEAD of running the radio action,
        // so the user can update paths without performing a workflow step.
        Dialog.addToSameRow();
        Dialog.addCheckbox("Configure paths", false);
        Dialog.addRadioButtonGroup("Action:", options, options.length, 1, options[2]);
        Dialog.show();  // Cancel here aborts the macro -> menu closes.

        wantConfigure = Dialog.getCheckbox();
        pick = Dialog.getRadioButton();

        if (wantConfigure) {
            cmdConfigure();
        } else if (startsWith(pick, "1.")) {
            cmdSetupProject();
        } else if (startsWith(pick, "2.")) {
            cmdEditParameters();
        } else if (startsWith(pick, "3.")) {
            cmdDrawROIs();
        } else if (startsWith(pick, "4.")) {
            cmdRunAnalysis();
        } else if (startsWith(pick, "5.")) {
            cmdReviewQC();
        } else if (startsWith(pick, "6.")) {
            cmdApplyCorrections();
        }
        // Loop back to the menu. To exit, click Cancel in the dialog.
    }
}

main();
