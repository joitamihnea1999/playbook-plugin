# Mind Map — Generation from Codebase

How to analyze a code repository and produce a populated `MIND_MAP.md`.
Three phases: scan the codebase, mine git history, construct the map.
See [mindmap.md](mindmap.md) for node format and quality criteria.

---

## Phase 1: Current State Analysis

### 1.1 Reconnaissance

Explore project structure broadly:

- Read README, docs, configuration files (package.json, pyproject.toml, Cargo.toml, etc.)
- Map top-level directories — what lives where
- Identify entry points (main, index, app)
- Note the technology stack from dependencies

### 1.2 Architecture Discovery

Trace the system's shape:

- **Components:** What are the major subsystems? How are they organized?
- **Data flow:** How does information move from input to output?
- **State:** Where is state managed? How do components communicate?
- **Boundaries:** Where does the system talk to the outside world? (APIs, DB, filesystem, network)

Read the critical files completely — don't skim. Core algorithms, state management,
API integration, data pipelines.

### 1.3 Feature Mapping

For each major feature:

- **What** it does
- **How** it's implemented (files, classes, functions)
- **Why** the design was chosen (look for comments, ADRs, commit messages)
- **Dependencies** — what it connects to

### 1.4 Implementation Details

Dive into specifics:

- Key algorithms and patterns
- Configuration and parameters
- Error handling strategies
- TODOs, FIXMEs, known issues

---

## Phase 2: Git History — mine the *why*, not the *when*

Read git history to **understand** how the architecture got its shape — but the map records the *decisions and their reasons*, never a timeline of hashes. The map is loaded every session; a "Development History" node full of `commit a1b2c3d (2024-03)` lines is log noise git already holds. What you extract here folds into the relevant subsystem node as a design lesson ("chose X over Y because Z"), not into a history node.

### 2.1 Timeline

```bash
# Full commit history with stats
git log --all --date=short --stat --pretty=format:"%n=== %h | %ad | %s ===" -- .

# First commit
git log --all --diff-filter=A --date=short --pretty=format:"%h | %ad | %s" -- . | tail -5

# Largest commits (most files changed)
git log --all --shortstat --oneline -- . | head -40
```

### 2.2 Development Phases

Identify from the timeline:

- **Initial creation** — what was the starting point?
- **Major refactors** — large insertions/deletions, file renames
- **Feature additions** — new files, new directories
- **Architecture shifts** — library changes, structural reorganization
- **Stabilization** — bug fixes, refinements, documentation

### 2.3 Commit Details

For each significant commit:

```bash
git show <hash> --stat    # what changed
git show <hash>           # how it changed
```

Extract the **design lesson**, not the log line: *why* a significant change was made and what it teaches about the current architecture. Fold that reason into the owning subsystem node ("switched to flat JSON so tasks stay hand-editable"). Leave the hash and date in git — they do not belong in the resident map.

---

## Phase 3: Construction

### 3.1 Plan Nodes

Before writing, outline the hierarchy:

1. **Nodes 1-5 (Foundation):** Project overview, core concept/theory, data flow, major architectural layers
2. **Nodes 6-15 (Systems):** One node per major subsystem or feature
3. **Nodes 16-20 (Implementation):** Tech stack, key design decisions (the *why*, not a commit timeline), workflow, design principles
4. **Nodes 21+ (Deep dives):** Specific algorithms, performance, specialized topics

Target scales with the codebase (see mindmap.md's Scale table): simple topics
15-30, standard projects 30-60, complex systems 60-150, large codebases 150+.
Under ~15 is usually too shallow; if a node is documenting rather than mapping
(exhaustive detail that belongs in overflow or the code), push it to overflow.

### 3.2 Write Nodes

For each node:

1. Clear noun-phrase title
2. Opening sentence defines the concept, links to parent nodes
3. Core content with specific details (file paths, function names, numbers)
4. Embed `[N]` links naturally throughout — don't list them at the end
5. Explain WHY, not just WHAT

### 3.3 Weave Links

After drafting all nodes:

1. Check every node has 2+ outgoing links
2. Add backward links — if [5] mentions [12], ensure [12] mentions [5]
3. Verify link clusters form around major concepts
4. Test navigation: can you follow links from [1] to any deep node?

### 3.4 Add Header

Prepend the standard AI agent header (see [mindmap.md](mindmap.md)).

### 3.5 Verify

- [ ] All major systems have nodes
- [ ] Every significant file/component mentioned somewhere
- [ ] Design decisions captured as reasons (no commit hashes / dates / history node — git holds the *when*)
- [ ] Every node has 2+ links
- [ ] Important nodes have 5+ links
- [ ] Links are bidirectional where appropriate
- [ ] 20-50 nodes total
