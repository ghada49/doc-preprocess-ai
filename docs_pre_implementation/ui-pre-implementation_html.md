

Use the HTML screens as the source of truth for the admin UI and write them as **requirements**, not as raw design notes.

Structure it like this:

# Admin UI / Product Requirements

## 1. Roles

Define:

* **Regular user**

  * submit jobs
  * view own jobs
  * view outputs
* **Admin**

  * all user capabilities if needed
  * view all jobs
  * correction queue
  * page correction workspace
  * lineage
  * shadow models
  * retraining
  * settings / operational tools

## 2. Admin screens

One section per screen:

* Dashboard
* Jobs List
* Correction Queue
* Correction Workspace

For each screen, write:

* purpose
* who can access it
* data displayed
* available actions
* backend dependencies
* empty/loading/error states

Example:

### Correction Queue

**Purpose**
Show all pages in `pending_human_correction`.

**Displayed data**

* page_id
* job_id
* collection
* review_reason
* best_available_output
* urgency / priority
* thumbnail preview

**Actions**

* open review workspace
* filter by reason / urgency / collection
* search by page_id / job_id

**Backend dependencies**

* `GET /v1/correction-queue`
* pagination
* filtering
* search

## 3. Correction workspace behavior

This is very important because your HTML already implies exact workflow.

Write requirements like:

* operator can view original OTIFF
* operator can switch to best available preprocessing output
* operator can inspect branch outputs IEP1A/1B/1C/1D
* operator can adjust crop box
* operator can adjust deskew angle
* operator can set split_x or clear split
* operator can submit correction
* operator can reject page
* operator can add reviewer notes

Then map these to backend:

* `POST /v1/jobs/{job_id}/pages/{page_number}/correction`
* `POST /v1/jobs/{job_id}/pages/{page_number}/correction-reject`

## 4. Permission matrix

Add a simple matrix:

* Dashboard: admin only
* Jobs list: admin only, or split admin/all-jobs vs user/my-jobs
* Correction queue: admin only
* Correction workspace: admin only
* Shadow models: admin only
* Retraining: admin only
* Lineage: admin only
* Job submission: regular user
* My jobs: regular user

## 5. API/UI mapping

This is the most useful section.

For each page, map UI widgets to existing or needed endpoints.

Example:

### Jobs page

Needs:

* `POST /v1/jobs`
* `GET /v1/jobs/{job_id}`
* likely new endpoint: `GET /v1/jobs?status=&pipeline_mode=&search=&page=`

### Dashboard

Needs aggregated endpoints, likely new:

* `GET /v1/admin/dashboard-summary`
* `GET /v1/admin/service-health`
* `GET /v1/admin/recent-jobs`

### Correction queue

Needs:

* `GET /v1/correction-queue`
* maybe add pagination/filter params

### Correction workspace

Needs:

* correction queue detail endpoint or lineage/job detail endpoint
* correction submit/reject endpoints

## What to change in `pre-implementation_spec.md`

Do not dump the HTML there directly.

Instead, add a new section near product/API requirements, such as:

## Admin Web Console

The system shall provide an admin web console with:

* operational dashboard
* jobs management
* correction queue
* page correction workspace
* lineage inspection
* shadow model monitoring
* retraining management

And another section:

## User Web Portal

The system shall provide a regular user portal with:

* job submission
* job status tracking
* output viewing

That keeps the implementation spec clean and product-level.

## What to change in `execution-checklist.md`

Add new tasks, probably in a new section.

Example:

## SECTION UI — WEB APPLICATION

* [ ] **UI.1 — Role model and access rules**

  * Define regular user vs admin permissions
  * Add backend authorization guard or placeholder role middleware

* [ ] **UI.2 — Admin dashboard API**

  * Add summary endpoints for throughput, active jobs, correction backlog, health, recent jobs

* [ ] **UI.3 — Admin jobs list API**

  * Add list endpoint with filtering, pagination, search

* [ ] **UI.4 — Correction queue API enhancements**

  * Ensure queue endpoint returns all fields needed by UI

* [ ] **UI.5 — Correction workspace detail API**

  * Add endpoint returning original image, best output, branch outputs, metadata, review reason

* [ ] **UI.6 — Correction submit / reject flow**

  * Wire frontend to correction endpoints

* [ ] **UI.7 — Admin frontend shell**

  * Shared sidebar, topbar, layout, auth-aware routing

* [ ] **UI.8 — Admin dashboard page**

* [ ] **UI.9 — Admin jobs page**

* [ ] **UI.10 — Correction queue page**

* [ ] **UI.11 — Correction workspace page**

* [ ] **UI.12 — Lineage page**

* [ ] **UI.13 — Shadow models page**

* [ ] **UI.14 — Retraining page**

* [ ] **UI.15 — Regular user portal**

  * submit job
  * my jobs
  * job detail / outputs

## Best practical recommendation

Do this in 3 files:

1. `frontend_product_spec.md`

   * screens
   * roles
   * actions
   * permissions
   * page requirements

2. update `pre-implementation_spec.md`

   * add concise sections for admin console and user portal

3. update `execution-checklist.md`

   * add concrete backend/frontend tasks


---

## 1) `admin-dashboard.html`

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta content="width=device-width, initial-scale=1.0" name="viewport" />
  <title>LibraryAI - Admin Dashboard</title>
  <script src="https://cdn.tailwindcss.com?plugins=forms,container-queries"></script>
  <link href="https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet" />
  <link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:wght@300;400;500;600;700" rel="stylesheet" />
  <script>
    tailwind.config = {
      darkMode: "class",
      theme: {
        extend: {
          colors: {
            primary: "#840131",
            "background-light": "#f8f5f6",
            "background-dark": "#230f16",
          },
          fontFamily: {
            display: ["Public Sans", "sans-serif"],
          },
          borderRadius: {
            DEFAULT: "0.125rem",
            lg: "0.25rem",
            xl: "0.5rem",
            full: "0.75rem",
          },
        },
      },
    };
  </script>
  <style>
    body { font-family: 'Public Sans', sans-serif; }
    .material-symbols-outlined {
      font-variation-settings: 'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24;
    }
  </style>
</head>
<body class="bg-background-light dark:bg-background-dark font-display text-slate-900 dark:text-slate-100">
  <div class="flex min-h-screen overflow-x-hidden">
    <aside class="w-64 border-r border-primary/10 bg-white dark:bg-background-dark/50 flex flex-col shrink-0">
      <div class="p-6 flex items-center gap-3 border-b border-primary/10">
        <div class="size-10 bg-primary flex items-center justify-center rounded-lg text-white">
          <span class="material-symbols-outlined">library_books</span>
        </div>
        <div>
          <h1 class="text-primary font-bold text-lg leading-tight">LibraryAI</h1>
          <p class="text-primary/60 text-xs font-medium uppercase tracking-wider">Admin Console</p>
        </div>
      </div>

      <nav class="flex-1 p-4 space-y-1">
        <a class="flex items-center gap-3 px-4 py-2.5 rounded-lg bg-primary text-white font-medium" href="#">
          <span class="material-symbols-outlined">dashboard</span>
          <span class="text-sm">Dashboard</span>
        </a>
        <a class="flex items-center gap-3 px-4 py-2.5 rounded-lg text-slate-600 dark:text-slate-400 hover:bg-primary/5 hover:text-primary transition-colors" href="#">
          <span class="material-symbols-outlined">work</span>
          <span class="text-sm">Jobs</span>
        </a>
        <a class="flex items-center gap-3 px-4 py-2.5 rounded-lg text-slate-600 dark:text-slate-400 hover:bg-primary/5 hover:text-primary transition-colors" href="#">
          <span class="material-symbols-outlined">rule</span>
          <span class="text-sm">Correction Queue</span>
        </a>
        <a class="flex items-center gap-3 px-4 py-2.5 rounded-lg text-slate-600 dark:text-slate-400 hover:bg-primary/5 hover:text-primary transition-colors" href="#">
          <span class="material-symbols-outlined">account_tree</span>
          <span class="text-sm">Lineage</span>
        </a>

        <div class="pt-4 pb-2 px-4">
          <p class="text-[10px] font-bold text-slate-400 uppercase tracking-widest">Advanced</p>
        </div>

        <a class="flex items-center gap-3 px-4 py-2.5 rounded-lg text-slate-600 dark:text-slate-400 hover:bg-primary/5 hover:text-primary transition-colors" href="#">
          <span class="material-symbols-outlined">analytics</span>
          <span class="text-sm">Shadow Models</span>
        </a>
        <a class="flex items-center gap-3 px-4 py-2.5 rounded-lg text-slate-600 dark:text-slate-400 hover:bg-primary/5 hover:text-primary transition-colors" href="#">
          <span class="material-symbols-outlined">model_training</span>
          <span class="text-sm">Retraining</span>
        </a>
        <a class="flex items-center gap-3 px-4 py-2.5 rounded-lg text-slate-600 dark:text-slate-400 hover:bg-primary/5 hover:text-primary transition-colors" href="#">
          <span class="material-symbols-outlined">settings</span>
          <span class="text-sm">Settings</span>
        </a>
      </nav>

      <div class="p-4 border-t border-primary/10">
        <div class="flex items-center gap-3 p-2">
          <div class="size-8 rounded-full bg-primary/20 flex items-center justify-center text-primary font-bold text-xs">AD</div>
          <div class="flex-1 min-w-0">
            <p class="text-xs font-semibold truncate">Admin User</p>
            <p class="text-[10px] text-slate-500 truncate">System Overseer</p>
          </div>
        </div>
      </div>
    </aside>

    <main class="flex-1 flex flex-col min-w-0 overflow-y-auto">
      <header class="h-16 border-b border-primary/10 bg-white dark:bg-background-dark/50 flex items-center justify-between px-8 sticky top-0 z-10">
        <div class="flex-1 max-w-xl">
          <div class="relative group">
            <span class="material-symbols-outlined absolute left-3 top-1/2 -translate-y-1/2 text-slate-400 group-focus-within:text-primary transition-colors">search</span>
            <input class="w-full bg-background-light dark:bg-slate-800/50 border-none rounded-lg pl-10 pr-4 py-2 text-sm focus:ring-2 focus:ring-primary/20 transition-all" placeholder="Search job ID, collection, document, or page..." type="text" />
          </div>
        </div>
        <div class="flex items-center gap-4">
          <button class="relative p-2 text-slate-500 hover:bg-primary/5 rounded-lg transition-colors">
            <span class="material-symbols-outlined">notifications</span>
            <span class="absolute top-2 right-2 size-2 bg-primary rounded-full border-2 border-white"></span>
          </button>
          <button class="p-2 text-slate-500 hover:bg-primary/5 rounded-lg transition-colors">
            <span class="material-symbols-outlined">help</span>
          </button>
          <div class="h-8 w-px bg-primary/10 mx-2"></div>
          <div class="size-10 rounded-full bg-primary/20 flex items-center justify-center text-primary font-bold text-xs">AD</div>
        </div>
      </header>

      <div class="p-8 space-y-8">
        <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-5 gap-6">
          <div class="bg-white dark:bg-background-dark/50 p-6 rounded-xl border border-primary/10 shadow-sm">
            <p class="text-xs font-bold text-slate-500 uppercase tracking-wider mb-1">Throughput</p>
            <h3 class="text-2xl font-bold">1.2k <span class="text-sm font-medium text-slate-500">pages/hr</span></h3>
            <div class="flex items-center gap-1 mt-4 text-emerald-600 text-sm font-semibold">
              <span class="material-symbols-outlined text-sm">trending_up</span>
              <span>12% from last hour</span>
            </div>
          </div>

          <div class="bg-white dark:bg-background-dark/50 p-6 rounded-xl border border-primary/10 shadow-sm">
            <p class="text-xs font-bold text-slate-500 uppercase tracking-wider mb-1">Auto Accept Rate</p>
            <h3 class="text-2xl font-bold">84.7%</h3>
            <div class="flex items-center gap-1 mt-4 text-emerald-600 text-sm font-semibold">
              <span class="material-symbols-outlined text-sm">check_circle</span>
              <span>Healthy</span>
            </div>
          </div>

          <div class="bg-white dark:bg-background-dark/50 p-6 rounded-xl border border-primary/10 shadow-sm">
            <p class="text-xs font-bold text-slate-500 uppercase tracking-wider mb-1">Pending Corrections</p>
            <h3 class="text-2xl font-bold">156</h3>
            <div class="flex items-center gap-1 mt-4 text-amber-600 text-sm font-semibold">
              <span class="material-symbols-outlined text-sm">warning</span>
              <span>Needs operator review</span>
            </div>
          </div>

          <div class="bg-white dark:bg-background-dark/50 p-6 rounded-xl border border-primary/10 shadow-sm">
            <p class="text-xs font-bold text-slate-500 uppercase tracking-wider mb-1">Active Jobs</p>
            <h3 class="text-2xl font-bold">42</h3>
            <div class="flex items-center gap-1 mt-4 text-primary text-sm font-semibold">
              <span class="material-symbols-outlined text-sm">sync</span>
              <span>8 workers active</span>
            </div>
          </div>

          <div class="bg-white dark:bg-background-dark/50 p-6 rounded-xl border border-primary/10 shadow-sm">
            <p class="text-xs font-bold text-slate-500 uppercase tracking-wider mb-1">Shadow Evaluations</p>
            <h3 class="text-2xl font-bold">318</h3>
            <div class="flex items-center gap-1 mt-4 text-sky-600 text-sm font-semibold">
              <span class="material-symbols-outlined text-sm">analytics</span>
              <span>Candidate under watch</span>
            </div>
          </div>
        </div>

        <div class="grid grid-cols-1 xl:grid-cols-3 gap-8">
          <div class="xl:col-span-1 space-y-8">
            <div class="bg-white dark:bg-background-dark/50 p-6 rounded-xl border border-primary/10 shadow-sm">
              <h2 class="text-lg font-bold mb-4 flex items-center gap-2">
                <span class="material-symbols-outlined text-primary">bolt</span>
                Quick Links
              </h2>
              <div class="space-y-3">
                <a class="flex items-center justify-between p-3 rounded-lg border border-primary/5 hover:bg-primary/5 transition-colors group" href="#">
                  <span class="text-sm font-medium">Open Correction Queue</span>
                  <span class="material-symbols-outlined text-sm text-slate-400 group-hover:text-primary">chevron_right</span>
                </a>
                <a class="flex items-center justify-between p-3 rounded-lg border border-primary/5 hover:bg-primary/5 transition-colors group" href="#">
                  <span class="text-sm font-medium">View Failed Jobs</span>
                  <span class="material-symbols-outlined text-sm text-slate-400 group-hover:text-primary">chevron_right</span>
                </a>
                <a class="flex items-center justify-between p-3 rounded-lg border border-primary/5 hover:bg-primary/5 transition-colors group" href="#">
                  <span class="text-sm font-medium">Inspect Shadow Models</span>
                  <span class="material-symbols-outlined text-sm text-slate-400 group-hover:text-primary">chevron_right</span>
                </a>
              </div>
            </div>

            <div class="bg-white dark:bg-background-dark/50 p-6 rounded-xl border border-primary/10 shadow-sm">
              <h2 class="text-lg font-bold mb-6 flex items-center gap-2">
                <span class="material-symbols-outlined text-primary">data_exploration</span>
                Pipeline Health
              </h2>
              <div class="space-y-5">
                <div class="space-y-2">
                  <div class="flex justify-between text-xs font-semibold">
                    <span>Preprocessing</span>
                    <span class="text-primary">91%</span>
                  </div>
                  <div class="h-2 w-full bg-primary/10 rounded-full overflow-hidden">
                    <div class="h-full bg-primary" style="width: 91%"></div>
                  </div>
                </div>
                <div class="space-y-2">
                  <div class="flex justify-between text-xs font-semibold">
                    <span>Rectification</span>
                    <span class="text-primary">74%</span>
                  </div>
                  <div class="h-2 w-full bg-primary/10 rounded-full overflow-hidden">
                    <div class="h-full bg-primary" style="width: 74%"></div>
                  </div>
                </div>
                <div class="space-y-2">
                  <div class="flex justify-between text-xs font-semibold">
                    <span>Layout Detection</span>
                    <span class="text-primary">86%</span>
                  </div>
                  <div class="h-2 w-full bg-primary/10 rounded-full overflow-hidden">
                    <div class="h-full bg-primary" style="width: 86%"></div>
                  </div>
                </div>
                <div class="space-y-2">
                  <div class="flex justify-between text-xs font-semibold">
                    <span>Human Review Throughput</span>
                    <span class="text-primary">63%</span>
                  </div>
                  <div class="h-2 w-full bg-primary/10 rounded-full overflow-hidden">
                    <div class="h-full bg-primary" style="width: 63%"></div>
                  </div>
                </div>
              </div>
            </div>

            <div class="bg-white dark:bg-background-dark/50 p-6 rounded-xl border border-primary/10 shadow-sm">
              <h2 class="text-lg font-bold mb-6 flex items-center gap-2">
                <span class="material-symbols-outlined text-primary">health_and_safety</span>
                Service Health
              </h2>
              <div class="grid grid-cols-2 gap-4">
                <div class="p-3 bg-emerald-50 rounded-lg border border-emerald-100">
                  <p class="text-[10px] font-bold text-emerald-700 uppercase">Postgres</p>
                  <p class="text-sm font-semibold flex items-center gap-1 mt-1"><span class="size-2 bg-emerald-500 rounded-full"></span>Stable</p>
                </div>
                <div class="p-3 bg-emerald-50 rounded-lg border border-emerald-100">
                  <p class="text-[10px] font-bold text-emerald-700 uppercase">Redis</p>
                  <p class="text-sm font-semibold flex items-center gap-1 mt-1"><span class="size-2 bg-emerald-500 rounded-full"></span>Healthy</p>
                </div>
                <div class="p-3 bg-emerald-50 rounded-lg border border-emerald-100">
                  <p class="text-[10px] font-bold text-emerald-700 uppercase">Workers</p>
                  <p class="text-sm font-semibold flex items-center gap-1 mt-1"><span class="size-2 bg-emerald-500 rounded-full"></span>12/12 Up</p>
                </div>
                <div class="p-3 bg-amber-50 rounded-lg border border-amber-100">
                  <p class="text-[10px] font-bold text-amber-700 uppercase">Storage</p>
                  <p class="text-sm font-semibold flex items-center gap-1 mt-1"><span class="size-2 bg-amber-500 rounded-full"></span>84% Full</p>
                </div>
              </div>
            </div>
          </div>

          <div class="xl:col-span-2">
            <div class="bg-white dark:bg-background-dark/50 rounded-xl border border-primary/10 shadow-sm overflow-hidden h-full flex flex-col">
              <div class="p-6 border-b border-primary/10 flex items-center justify-between">
                <h2 class="text-lg font-bold flex items-center gap-2">
                  <span class="material-symbols-outlined text-primary">list_alt</span>
                  Recent Jobs
                </h2>
                <button class="text-primary text-sm font-bold hover:underline">View All Jobs</button>
              </div>

              <div class="overflow-x-auto flex-1">
                <table class="w-full text-left border-collapse">
                  <thead>
                    <tr class="bg-primary/5 text-slate-500 text-[10px] font-bold uppercase tracking-widest">
                      <th class="px-6 py-4">Job ID</th>
                      <th class="px-6 py-4">Collection</th>
                      <th class="px-6 py-4">Stage</th>
                      <th class="px-6 py-4">Status</th>
                      <th class="px-6 py-4">Attention</th>
                      <th class="px-6 py-4 text-right">Action</th>
                    </tr>
                  </thead>
                  <tbody class="divide-y divide-primary/5">
                    <tr class="hover:bg-primary/5 transition-colors">
                      <td class="px-6 py-4 font-mono text-xs">JOB-22485</td>
                      <td class="px-6 py-4 text-sm font-medium">National Archives</td>
                      <td class="px-6 py-4 text-xs font-medium">layout_detection</td>
                      <td class="px-6 py-4">
                        <span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[10px] font-bold bg-blue-100 text-blue-700">
                          <span class="size-1.5 bg-blue-500 rounded-full animate-pulse"></span>
                          RUNNING
                        </span>
                      </td>
                      <td class="px-6 py-4 text-xs text-slate-500">—</td>
                      <td class="px-6 py-4 text-right"><button class="material-symbols-outlined text-slate-400 hover:text-primary transition-colors">visibility</button></td>
                    </tr>
                    <tr class="hover:bg-primary/5 transition-colors">
                      <td class="px-6 py-4 font-mono text-xs">JOB-22484</td>
                      <td class="px-6 py-4 text-sm font-medium">Census Records</td>
                      <td class="px-6 py-4 text-xs font-medium">accepted</td>
                      <td class="px-6 py-4">
                        <span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[10px] font-bold bg-emerald-100 text-emerald-700">
                          <span class="size-1.5 bg-emerald-500 rounded-full"></span>
                          DONE
                        </span>
                      </td>
                      <td class="px-6 py-4 text-xs text-slate-500">—</td>
                      <td class="px-6 py-4 text-right"><button class="material-symbols-outlined text-slate-400 hover:text-primary transition-colors">visibility</button></td>
                    </tr>
                    <tr class="hover:bg-primary/5 transition-colors">
                      <td class="px-6 py-4 font-mono text-xs">JOB-22483</td>
                      <td class="px-6 py-4 text-sm font-medium">Handwritten Journals</td>
                      <td class="px-6 py-4 text-xs font-medium">pending_human_correction</td>
                      <td class="px-6 py-4">
                        <span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[10px] font-bold bg-amber-100 text-amber-700">
                          <span class="size-1.5 bg-amber-500 rounded-full"></span>
                          REVIEW
                        </span>
                      </td>
                      <td class="px-6 py-4 text-xs text-amber-700 font-semibold">Needs correction</td>
                      <td class="px-6 py-4 text-right"><button class="material-symbols-outlined text-slate-400 hover:text-primary transition-colors">visibility</button></td>
                    </tr>
                    <tr class="hover:bg-primary/5 transition-colors">
                      <td class="px-6 py-4 font-mono text-xs">JOB-22482</td>
                      <td class="px-6 py-4 text-sm font-medium">Legal Deposit</td>
                      <td class="px-6 py-4 text-xs font-medium">preprocessing_complete</td>
                      <td class="px-6 py-4">
                        <span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[10px] font-bold bg-emerald-100 text-emerald-700">
                          <span class="size-1.5 bg-emerald-500 rounded-full"></span>
                          DONE
                        </span>
                      </td>
                      <td class="px-6 py-4 text-xs text-slate-500">Preprocess-only job</td>
                      <td class="px-6 py-4 text-right"><button class="material-symbols-outlined text-slate-400 hover:text-primary transition-colors">visibility</button></td>
                    </tr>
                    <tr class="hover:bg-primary/5 transition-colors">
                      <td class="px-6 py-4 font-mono text-xs">JOB-22481</td>
                      <td class="px-6 py-4 text-sm font-medium">Manuscript Fragments</td>
                      <td class="px-6 py-4 text-xs font-medium">failed</td>
                      <td class="px-6 py-4">
                        <span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[10px] font-bold bg-rose-100 text-rose-700">
                          <span class="size-1.5 bg-rose-500 rounded-full"></span>
                          FAILED
                        </span>
                      </td>
                      <td class="px-6 py-4 text-xs text-rose-700 font-semibold">Retry required</td>
                      <td class="px-6 py-4 text-right"><button class="material-symbols-outlined text-slate-400 hover:text-primary transition-colors">visibility</button></td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
      </div>
    </main>
  </div>
</body>
</html>
```

---

## 2) `admin-jobs.html`

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta content="width=device-width, initial-scale=1.0" name="viewport" />
  <title>LibraryAI - Jobs</title>
  <script src="https://cdn.tailwindcss.com?plugins=forms,container-queries"></script>
  <link href="https://fonts.googleapis.com/css2?family=Public+Sans:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet" />
  <link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:wght@300;400;500;600;700&display=swap" rel="stylesheet" />
  <script>
    tailwind.config = {
      darkMode: "class",
      theme: {
        extend: {
          colors: {
            primary: "#840131",
            "background-light": "#f8f5f6",
            "background-dark": "#230f16",
          },
          fontFamily: {
            display: ["Public Sans", "sans-serif"],
          },
        },
      },
    };
  </script>
  <style>
    body { font-family: 'Public Sans', sans-serif; }
    .material-symbols-outlined {
      font-variation-settings: 'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24;
    }
  </style>
</head>
<body class="bg-background-light dark:bg-background-dark text-slate-900 dark:text-slate-100 font-display min-h-screen">
  <div class="flex h-screen overflow-hidden">
    <aside class="w-64 flex-shrink-0 border-r border-primary/10 bg-white dark:bg-background-dark/50 flex flex-col">
      <div class="p-6 flex items-center gap-3">
        <div class="size-10 bg-primary rounded-xl flex items-center justify-center text-white">
          <span class="material-symbols-outlined text-2xl">library_books</span>
        </div>
        <div class="flex flex-col">
          <h1 class="text-primary font-bold text-sm uppercase tracking-wider">LibraryAI</h1>
          <p class="text-slate-500 text-xs font-medium">Admin Console</p>
        </div>
      </div>

      <nav class="flex-1 px-4 space-y-1 overflow-y-auto mt-4">
        <a class="flex items-center gap-3 px-3 py-2.5 text-slate-600 hover:bg-primary/5 rounded-lg transition-colors group" href="#"><span class="material-symbols-outlined group-hover:text-primary">dashboard</span><span class="text-sm font-semibold">Dashboard</span></a>
        <a class="flex items-center gap-3 px-3 py-2.5 bg-primary text-white rounded-lg shadow-sm shadow-primary/20" href="#"><span class="material-symbols-outlined">work</span><span class="text-sm font-semibold">Jobs</span></a>
        <a class="flex items-center gap-3 px-3 py-2.5 text-slate-600 hover:bg-primary/5 rounded-lg transition-colors group" href="#"><span class="material-symbols-outlined group-hover:text-primary">rule</span><span class="text-sm font-semibold">Correction Queue</span></a>
        <a class="flex items-center gap-3 px-3 py-2.5 text-slate-600 hover:bg-primary/5 rounded-lg transition-colors group" href="#"><span class="material-symbols-outlined group-hover:text-primary">account_tree</span><span class="text-sm font-semibold">Lineage</span></a>

        <div class="pt-4 pb-2 px-3">
          <p class="text-[10px] uppercase font-bold text-slate-400 tracking-widest">Advanced</p>
        </div>

        <a class="flex items-center gap-3 px-3 py-2.5 text-slate-600 hover:bg-primary/5 rounded-lg transition-colors group" href="#"><span class="material-symbols-outlined group-hover:text-primary">analytics</span><span class="text-sm font-semibold">Shadow Models</span></a>
        <a class="flex items-center gap-3 px-3 py-2.5 text-slate-600 hover:bg-primary/5 rounded-lg transition-colors group" href="#"><span class="material-symbols-outlined group-hover:text-primary">model_training</span><span class="text-sm font-semibold">Retraining</span></a>
      </nav>

      <div class="p-4 border-t border-primary/10">
        <a class="flex items-center gap-3 px-3 py-2.5 text-slate-600 hover:bg-primary/5 rounded-lg transition-colors group" href="#">
          <span class="material-symbols-outlined group-hover:text-primary">settings</span>
          <span class="text-sm font-semibold">Settings</span>
        </a>
        <div class="mt-4 flex items-center gap-3 px-3 py-4 bg-primary/5 rounded-xl border border-primary/10">
          <div class="size-8 rounded-full bg-primary/20 flex items-center justify-center text-primary font-bold text-xs">AD</div>
          <div class="flex-1 min-w-0">
            <p class="text-xs font-bold truncate">Admin User</p>
            <p class="text-[10px] text-slate-500 truncate">System Overseer</p>
          </div>
          <span class="material-symbols-outlined text-slate-400 text-sm">more_vert</span>
        </div>
      </div>
    </aside>

    <main class="flex-1 flex flex-col overflow-hidden">
      <header class="h-16 border-b border-primary/10 bg-white px-8 flex items-center justify-between">
        <div class="flex items-center gap-4 flex-1">
          <div class="relative max-w-md w-full">
            <span class="material-symbols-outlined absolute left-3 top-1/2 -translate-y-1/2 text-slate-400 text-lg">search</span>
            <input class="w-full bg-primary/5 border-none rounded-lg pl-10 pr-4 py-2 text-sm focus:ring-2 focus:ring-primary/20 placeholder:text-slate-400" placeholder="Search job ID, collection, source, or page status..." type="text" />
          </div>
        </div>
        <div class="flex items-center gap-4">
          <button class="size-9 rounded-lg hover:bg-primary/5 flex items-center justify-center text-slate-600"><span class="material-symbols-outlined">notifications</span></button>
          <button class="size-9 rounded-lg hover:bg-primary/5 flex items-center justify-center text-slate-600"><span class="material-symbols-outlined">help</span></button>
          <div class="h-8 w-[1px] bg-primary/10 mx-2"></div>
          <button class="bg-primary text-white text-sm font-bold px-4 py-2 rounded-lg flex items-center gap-2">
            <span class="material-symbols-outlined text-lg leading-none">add</span>
            New Ingestion Job
          </button>
        </div>
      </header>

      <div class="p-8 flex-1 flex flex-col overflow-hidden">
        <div class="mb-6 flex flex-col md:flex-row md:items-end justify-between gap-4">
          <div>
            <h2 class="text-3xl font-black tracking-tight">Jobs</h2>
            <p class="text-slate-500 mt-1">Operational list of ingestion runs and page outcomes.</p>
          </div>

          <div class="flex gap-2 flex-wrap">
            <div class="flex items-center gap-2 bg-white border border-primary/10 px-3 py-1.5 rounded-lg shadow-sm">
              <span class="text-xs font-bold text-slate-500 uppercase tracking-wider">Status</span>
              <select class="border-none bg-transparent text-sm font-semibold focus:ring-0 py-0 pl-1 pr-8">
                <option>All</option>
                <option>Running</option>
                <option>Done</option>
                <option>Failed</option>
              </select>
            </div>
            <div class="flex items-center gap-2 bg-white border border-primary/10 px-3 py-1.5 rounded-lg shadow-sm">
              <span class="text-xs font-bold text-slate-500 uppercase tracking-wider">Pipeline</span>
              <select class="border-none bg-transparent text-sm font-semibold focus:ring-0 py-0 pl-1 pr-8">
                <option>All Modes</option>
                <option>layout</option>
                <option>preprocess</option>
              </select>
            </div>
            <div class="flex items-center gap-2 bg-white border border-primary/10 px-3 py-1.5 rounded-lg shadow-sm">
              <span class="text-xs font-bold text-slate-500 uppercase tracking-wider">Attention</span>
              <select class="border-none bg-transparent text-sm font-semibold focus:ring-0 py-0 pl-1 pr-8">
                <option>All Jobs</option>
                <option>Needs Review</option>
                <option>Has Failures</option>
              </select>
            </div>
            <div class="flex items-center gap-2 bg-white border border-primary/10 px-3 py-1.5 rounded-lg shadow-sm">
              <span class="material-symbols-outlined text-slate-400 text-sm">calendar_today</span>
              <select class="border-none bg-transparent text-sm font-semibold focus:ring-0 py-0 pl-1 pr-8">
                <option>Last 24 Hours</option>
                <option>Last 7 Days</option>
                <option>Last 30 Days</option>
              </select>
            </div>
          </div>
        </div>

        <div class="flex-1 bg-white border border-primary/10 rounded-xl overflow-hidden shadow-sm flex flex-col">
          <div class="overflow-auto flex-1">
            <table class="w-full text-left border-collapse min-w-[1250px]">
              <thead class="sticky top-0 bg-slate-50 border-b border-primary/10 z-10">
                <tr>
                  <th class="px-4 py-3 text-xs font-bold text-slate-500 uppercase tracking-wider">Job ID</th>
                  <th class="px-4 py-3 text-xs font-bold text-slate-500 uppercase tracking-wider">Collection</th>
                  <th class="px-4 py-3 text-xs font-bold text-slate-500 uppercase tracking-wider">Source</th>
                  <th class="px-4 py-3 text-xs font-bold text-slate-500 uppercase tracking-wider">Pipeline Mode</th>
                  <th class="px-4 py-3 text-xs font-bold text-slate-500 uppercase tracking-wider">Current Stage</th>
                  <th class="px-4 py-3 text-xs font-bold text-slate-500 uppercase tracking-wider">Job Status</th>
                  <th class="px-4 py-3 text-xs font-bold text-slate-500 uppercase tracking-wider text-center">Page Outcomes<span class="block text-[8px] font-normal lowercase opacity-70">accepted / review / failed</span></th>
                  <th class="px-4 py-3 text-xs font-bold text-slate-500 uppercase tracking-wider">Needs Attention</th>
                  <th class="px-4 py-3 text-xs font-bold text-slate-500 uppercase tracking-wider">Updated</th>
                  <th class="px-4 py-3 text-xs font-bold text-slate-500 uppercase tracking-wider text-right">Actions</th>
                </tr>
              </thead>

              <tbody class="divide-y divide-primary/5">
                <tr class="hover:bg-primary/5 transition-colors group">
                  <td class="px-4 py-3 text-sm font-bold text-primary">JOB-2023-9081</td>
                  <td class="px-4 py-3 text-sm text-slate-600">Preprint Archives</td>
                  <td class="px-4 py-3 text-sm">ArXiv Repository</td>
                  <td class="px-4 py-3 text-sm font-mono text-slate-500">layout</td>
                  <td class="px-4 py-3 text-sm">accepted</td>
                  <td class="px-4 py-3"><span class="px-2 py-1 rounded text-[10px] font-black uppercase tracking-widest bg-emerald-100 text-emerald-700">Done</span></td>
                  <td class="px-4 py-3 text-xs text-center font-medium font-mono"><span class="text-emerald-600">140</span> / <span class="text-amber-600">2</span> / <span class="text-rose-600">0</span></td>
                  <td class="px-4 py-3 text-sm text-slate-400">—</td>
                  <td class="px-4 py-3 text-sm text-slate-500">Oct 24, 15:02</td>
                  <td class="px-4 py-3 text-right">
                    <div class="flex items-center justify-end gap-2">
                      <button class="material-symbols-outlined text-slate-400 hover:text-primary text-lg" title="View Job">visibility</button>
                      <button class="material-symbols-outlined text-slate-400 hover:text-primary text-lg" title="Open Lineage">account_tree</button>
                      <button class="material-symbols-outlined text-slate-400 hover:text-primary text-lg" title="Open Corrections">rule</button>
                      <button class="material-symbols-outlined text-slate-300 text-lg cursor-not-allowed" title="Retry Failed Pages">replay</button>
                    </div>
                  </td>
                </tr>

                <tr class="hover:bg-primary/5 transition-colors group bg-primary/5">
                  <td class="px-4 py-3 text-sm font-bold text-primary">JOB-2023-9082</td>
                  <td class="px-4 py-3 text-sm text-slate-600">Public Domain Books</td>
                  <td class="px-4 py-3 text-sm">Gutenberg Project</td>
                  <td class="px-4 py-3 text-sm font-mono text-slate-500">layout</td>
                  <td class="px-4 py-3 text-sm">layout_detection</td>
                  <td class="px-4 py-3"><span class="px-2 py-1 rounded text-[10px] font-black uppercase tracking-widest bg-blue-100 text-blue-700">Running</span></td>
                  <td class="px-4 py-3 text-xs text-center font-medium font-mono"><span class="text-emerald-600">140</span> / <span class="text-amber-600">2</span> / <span class="text-rose-600">0</span></td>
                  <td class="px-4 py-3 text-sm text-amber-700 font-semibold">Review backlog</td>
                  <td class="px-4 py-3 text-sm text-slate-500">Oct 24, 15:10</td>
                  <td class="px-4 py-3 text-right">
                    <div class="flex items-center justify-end gap-2">
                      <button class="material-symbols-outlined text-slate-400 hover:text-primary text-lg" title="View Job">visibility</button>
                      <button class="material-symbols-outlined text-slate-400 hover:text-primary text-lg" title="Open Lineage">account_tree</button>
                      <button class="material-symbols-outlined text-slate-400 hover:text-primary text-lg" title="Open Corrections">rule</button>
                      <button class="material-symbols-outlined text-slate-300 text-lg cursor-not-allowed" title="Retry Failed Pages">replay</button>
                    </div>
                  </td>
                </tr>

                <tr class="hover:bg-primary/5 transition-colors group">
                  <td class="px-4 py-3 text-sm font-bold text-primary">JOB-2023-9084</td>
                  <td class="px-4 py-3 text-sm text-slate-600">Historical Records</td>
                  <td class="px-4 py-3 text-sm">National Archives</td>
                  <td class="px-4 py-3 text-sm font-mono text-slate-500">layout</td>
                  <td class="px-4 py-3 text-sm">failed</td>
                  <td class="px-4 py-3"><span class="px-2 py-1 rounded text-[10px] font-black uppercase tracking-widest bg-rose-100 text-rose-700">Failed</span></td>
                  <td class="px-4 py-3 text-xs text-center font-medium font-mono"><span class="text-emerald-600">85</span> / <span class="text-amber-600">12</span> / <span class="text-rose-600">12</span></td>
                  <td class="px-4 py-3 text-sm text-rose-700 font-semibold">Retry required</td>
                  <td class="px-4 py-3 text-sm text-slate-500">Oct 24, 15:45</td>
                  <td class="px-4 py-3 text-right">
                    <div class="flex items-center justify-end gap-2">
                      <button class="material-symbols-outlined text-slate-400 hover:text-primary text-lg" title="View Job">visibility</button>
                      <button class="material-symbols-outlined text-slate-400 hover:text-primary text-lg" title="Open Lineage">account_tree</button>
                      <button class="material-symbols-outlined text-slate-400 hover:text-primary text-lg" title="Open Corrections">rule</button>
                      <button class="material-symbols-outlined text-primary text-lg" title="Retry Failed Pages">replay</button>
                    </div>
                  </td>
                </tr>

                <tr class="hover:bg-primary/5 transition-colors group">
                  <td class="px-4 py-3 text-sm font-bold text-primary">JOB-2023-9086</td>
                  <td class="px-4 py-3 text-sm text-slate-600">California Digital</td>
                  <td class="px-4 py-3 text-sm">Stanford Digital</td>
                  <td class="px-4 py-3 text-sm font-mono text-slate-500">layout</td>
                  <td class="px-4 py-3 text-sm">pending_human_correction</td>
                  <td class="px-4 py-3"><span class="px-2 py-1 rounded text-[10px] font-black uppercase tracking-widest bg-amber-100 text-amber-700">Review</span></td>
                  <td class="px-4 py-3 text-xs text-center font-medium font-mono"><span class="text-emerald-600">110</span> / <span class="text-amber-600">18</span> / <span class="text-rose-600">3</span></td>
                  <td class="px-4 py-3 text-sm text-amber-700 font-semibold">Needs human correction</td>
                  <td class="px-4 py-3 text-sm text-slate-500">Oct 24, 16:12</td>
                  <td class="px-4 py-3 text-right">
                    <div class="flex items-center justify-end gap-2">
                      <button class="material-symbols-outlined text-slate-400 hover:text-primary text-lg" title="View Job">visibility</button>
                      <button class="material-symbols-outlined text-slate-400 hover:text-primary text-lg" title="Open Lineage">account_tree</button>
                      <button class="material-symbols-outlined text-primary text-lg" title="Open Corrections">rule</button>
                      <button class="material-symbols-outlined text-slate-300 text-lg cursor-not-allowed" title="Retry Failed Pages">replay</button>
                    </div>
                  </td>
                </tr>

                <tr class="hover:bg-primary/5 transition-colors group">
                  <td class="px-4 py-3 text-sm font-bold text-primary">JOB-2023-9088</td>
                  <td class="px-4 py-3 text-sm text-slate-600">Internal Scan Log</td>
                  <td class="px-4 py-3 text-sm">Internal Digits</td>
                  <td class="px-4 py-3 text-sm font-mono text-slate-500">preprocess</td>
                  <td class="px-4 py-3 text-sm">preprocessing_complete</td>
                  <td class="px-4 py-3"><span class="px-2 py-1 rounded text-[10px] font-black uppercase tracking-widest bg-emerald-100 text-emerald-700">Done</span></td>
                  <td class="px-4 py-3 text-xs text-center font-medium font-mono"><span class="text-emerald-600">0</span> / <span class="text-amber-600">0</span> / <span class="text-rose-600">0</span></td>
                  <td class="px-4 py-3 text-sm text-slate-500">Preprocess-only</td>
                  <td class="px-4 py-3 text-sm text-slate-500">Oct 24, 17:01</td>
                  <td class="px-4 py-3 text-right">
                    <div class="flex items-center justify-end gap-2">
                      <button class="material-symbols-outlined text-slate-400 hover:text-primary text-lg" title="View Job">visibility</button>
                      <button class="material-symbols-outlined text-slate-400 hover:text-primary text-lg" title="Open Lineage">account_tree</button>
                      <button class="material-symbols-outlined text-slate-400 hover:text-primary text-lg" title="Open Corrections">rule</button>
                      <button class="material-symbols-outlined text-slate-300 text-lg cursor-not-allowed" title="Retry Failed Pages">replay</button>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="bg-slate-50 border-t border-primary/10 px-4 py-3 flex items-center justify-between">
            <p class="text-xs font-medium text-slate-500">Showing <span class="text-slate-900 font-bold">1-5</span> of <span class="text-slate-900 font-bold">142</span> jobs</p>
            <div class="flex gap-1">
              <button class="px-3 py-1 bg-white border border-primary/10 rounded text-xs font-bold hover:bg-primary/5 disabled:opacity-50" disabled>Previous</button>
              <button class="px-3 py-1 bg-primary text-white border border-primary rounded text-xs font-bold">1</button>
              <button class="px-3 py-1 bg-white border border-primary/10 rounded text-xs font-bold hover:bg-primary/5">2</button>
              <button class="px-3 py-1 bg-white border border-primary/10 rounded text-xs font-bold hover:bg-primary/5">3</button>
              <span class="px-2 py-1 text-xs">...</span>
              <button class="px-3 py-1 bg-white border border-primary/10 rounded text-xs font-bold hover:bg-primary/5">18</button>
              <button class="px-3 py-1 bg-white border border-primary/10 rounded text-xs font-bold hover:bg-primary/5">Next</button>
            </div>
          </div>
        </div>
      </div>
    </main>
  </div>
</body>
</html>
```

---

## 3) `admin-correction-queue.html`

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta content="width=device-width, initial-scale=1.0" name="viewport" />
  <title>LibraryAI - Correction Queue</title>
  <script src="https://cdn.tailwindcss.com?plugins=forms,container-queries"></script>
  <link href="https://fonts.googleapis.com/css2?family=Public+Sans:wght@300;400;500;600;700;800&display=swap" rel="stylesheet" />
  <link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:wght@300;400;500;600;700" rel="stylesheet" />
  <script>
    tailwind.config = {
      darkMode: "class",
      theme: {
        extend: {
          colors: {
            primary: "#840131",
            "background-light": "#f8f5f6",
            "background-dark": "#230f16",
          },
          fontFamily: {
            display: ["Public Sans", "sans-serif"],
          },
        },
      },
    };
  </script>
  <style>
    body { font-family: "Public Sans", sans-serif; }
    .material-symbols-outlined {
      font-variation-settings: 'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24;
    }
  </style>
</head>
<body class="bg-background-light text-slate-900">
  <div class="relative flex h-screen w-full overflow-hidden">
    <aside class="w-64 flex-shrink-0 border-r border-primary/10 bg-white flex flex-col">
      <div class="p-6 flex items-center gap-3">
        <div class="size-8 bg-primary rounded flex items-center justify-center text-white">
          <span class="material-symbols-outlined text-xl">library_books</span>
        </div>
        <h2 class="text-primary text-lg font-bold tracking-tight">LibraryAI</h2>
      </div>

      <nav class="flex-1 px-4 space-y-1 overflow-y-auto">
        <a class="flex items-center gap-3 px-3 py-2.5 rounded hover:bg-primary/5 text-slate-700" href="#"><span class="material-symbols-outlined text-[20px]">dashboard</span><span class="text-sm font-medium">Dashboard</span></a>
        <a class="flex items-center gap-3 px-3 py-2.5 rounded hover:bg-primary/5 text-slate-700" href="#"><span class="material-symbols-outlined text-[20px]">work</span><span class="text-sm font-medium">Jobs</span></a>
        <a class="flex items-center gap-3 px-3 py-2.5 rounded bg-primary text-white" href="#"><span class="material-symbols-outlined text-[20px]">rule</span><span class="text-sm font-medium">Correction Queue</span></a>
        <a class="flex items-center gap-3 px-3 py-2.5 rounded hover:bg-primary/5 text-slate-700" href="#"><span class="material-symbols-outlined text-[20px]">account_tree</span><span class="text-sm font-medium">Lineage</span></a>

        <div class="pt-6 pb-2 px-2">
          <p class="text-[10px] uppercase font-bold text-primary/60 tracking-widest">Advanced</p>
        </div>

        <a class="flex items-center gap-3 px-3 py-2.5 rounded hover:bg-primary/5 text-slate-700" href="#"><span class="material-symbols-outlined text-[20px]">analytics</span><span class="text-sm font-medium">Shadow Models</span></a>
        <a class="flex items-center gap-3 px-3 py-2.5 rounded hover:bg-primary/5 text-slate-700" href="#"><span class="material-symbols-outlined text-[20px]">model_training</span><span class="text-sm font-medium">Retraining</span></a>
      </nav>

      <div class="p-4 border-t border-primary/10">
        <div class="flex items-center gap-3 p-2">
          <div class="size-9 rounded-full bg-primary/20 flex items-center justify-center text-primary font-bold text-xs">AD</div>
          <div class="flex-1 min-w-0">
            <p class="text-xs font-bold truncate">Admin User</p>
            <p class="text-[10px] text-primary/70">Review Operator</p>
          </div>
          <span class="material-symbols-outlined text-sm opacity-50">more_vert</span>
        </div>
      </div>
    </aside>

    <main class="flex-1 flex flex-col min-w-0 overflow-hidden">
      <header class="h-16 border-b border-primary/10 bg-white flex items-center justify-between px-8 z-10">
        <div class="flex items-center flex-1 max-w-md">
          <div class="relative w-full">
            <span class="material-symbols-outlined absolute left-3 top-1/2 -translate-y-1/2 text-primary/50 text-xl">search</span>
            <input class="w-full pl-10 pr-4 py-2 bg-primary/5 border-none rounded text-sm focus:ring-1 focus:ring-primary" placeholder="Search by page ID, job ID, collection, or reason..." type="text" />
          </div>
        </div>
        <div class="flex items-center gap-4">
          <button class="size-10 flex items-center justify-center rounded hover:bg-primary/5 text-primary/70 relative">
            <span class="material-symbols-outlined">notifications</span>
            <span class="absolute top-2.5 right-2.5 size-2 bg-primary rounded-full border-2 border-white"></span>
          </button>
          <button class="size-10 flex items-center justify-center rounded hover:bg-primary/5 text-primary/70">
            <span class="material-symbols-outlined">help_outline</span>
          </button>
          <div class="h-6 w-px bg-primary/10 mx-2"></div>
          <button class="bg-primary text-white px-4 py-2 rounded text-sm font-bold flex items-center gap-2 hover:bg-primary/90">
            <span class="material-symbols-outlined text-sm">play_arrow</span>
            Start Review
          </button>
        </div>
      </header>

      <div class="flex-1 overflow-y-auto p-8">
        <div class="mb-8">
          <div class="flex items-end justify-between">
            <div>
              <h1 class="text-2xl font-bold tracking-tight">Correction Queue</h1>
              <p class="text-slate-500 text-sm mt-1">Pages in <code>pending_human_correction</code> waiting for crop, deskew, or split review.</p>
            </div>
            <div class="flex gap-2">
              <div class="flex items-center bg-white border border-primary/10 rounded-lg p-1">
                <button class="px-4 py-1.5 text-xs font-bold rounded bg-primary text-white">All (42)</button>
                <button class="px-4 py-1.5 text-xs font-bold rounded text-slate-500 hover:text-primary">Critical (8)</button>
                <button class="px-4 py-1.5 text-xs font-bold rounded text-slate-500 hover:text-primary">In Progress</button>
              </div>
              <button class="flex items-center gap-2 px-3 py-1.5 border border-primary/10 rounded-lg bg-white text-xs font-bold">
                <span class="material-symbols-outlined text-sm">filter_list</span>
                Filter
              </button>
            </div>
          </div>
        </div>

        <div class="bg-white border border-primary/10 rounded-xl overflow-hidden">
          <table class="w-full text-left border-collapse">
            <thead>
              <tr class="bg-primary/5 border-b border-primary/10">
                <th class="px-6 py-4 text-[11px] font-bold uppercase tracking-wider text-primary/70 w-24">Thumbnail</th>
                <th class="px-6 py-4 text-[11px] font-bold uppercase tracking-wider text-primary/70">Page ID</th>
                <th class="px-6 py-4 text-[11px] font-bold uppercase tracking-wider text-primary/70">Job ID</th>
                <th class="px-6 py-4 text-[11px] font-bold uppercase tracking-wider text-primary/70">Review Reason</th>
                <th class="px-6 py-4 text-[11px] font-bold uppercase tracking-wider text-primary/70">Best Available Output</th>
                <th class="px-6 py-4 text-[11px] font-bold uppercase tracking-wider text-primary/70">Priority</th>
                <th class="px-6 py-4 text-[11px] font-bold uppercase tracking-wider text-primary/70 text-right">Action</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-primary/5">
              <tr class="hover:bg-primary/[0.02] transition-colors group">
                <td class="px-6 py-4"><div class="size-14 bg-slate-100 rounded border border-primary/5"></div></td>
                <td class="px-6 py-4"><div class="flex flex-col"><span class="text-sm font-bold">PAGE-9842</span><span class="text-[10px] text-slate-500 uppercase font-medium">Manuscript Collection</span></div></td>
                <td class="px-6 py-4 text-sm font-mono">JOB-22483</td>
                <td class="px-6 py-4 text-sm">preprocessing_consensus_failed</td>
                <td class="px-6 py-4 text-xs text-slate-500">IEP1C selected</td>
                <td class="px-6 py-4"><span class="px-2.5 py-1 rounded-full bg-red-100 text-red-700 text-[10px] font-bold uppercase tracking-wide">High</span></td>
                <td class="px-6 py-4 text-right"><button class="text-primary font-bold text-xs hover:underline">Review</button></td>
              </tr>

              <tr class="hover:bg-primary/[0.02] transition-colors group">
                <td class="px-6 py-4"><div class="size-14 bg-slate-100 rounded border border-primary/5"></div></td>
                <td class="px-6 py-4"><div class="flex flex-col"><span class="text-sm font-bold">PAGE-8731</span><span class="text-[10px] text-slate-500 uppercase font-medium">Historical Archives</span></div></td>
                <td class="px-6 py-4 text-sm font-mono">JOB-22490</td>
                <td class="px-6 py-4 text-sm">rectification_failed_or_low_confidence</td>
                <td class="px-6 py-4 text-xs text-slate-500">IEP1A selected</td>
                <td class="px-6 py-4"><span class="px-2.5 py-1 rounded-full bg-orange-100 text-orange-700 text-[10px] font-bold uppercase tracking-wide">Medium</span></td>
                <td class="px-6 py-4 text-right"><button class="text-primary font-bold text-xs hover:underline">Review</button></td>
              </tr>

              <tr class="hover:bg-primary/[0.02] transition-colors group">
                <td class="px-6 py-4"><div class="size-14 bg-slate-100 rounded border border-primary/5"></div></td>
                <td class="px-6 py-4"><div class="flex flex-col"><span class="text-sm font-bold">PAGE-7720</span><span class="text-[10px] text-slate-500 uppercase font-medium">Academic Journals</span></div></td>
                <td class="px-6 py-4 text-sm font-mono">JOB-22491</td>
                <td class="px-6 py-4 text-sm">split_verification_needed</td>
                <td class="px-6 py-4 text-xs text-slate-500">IEP1B selected</td>
                <td class="px-6 py-4"><span class="px-2.5 py-1 rounded-full bg-slate-100 text-slate-700 text-[10px] font-bold uppercase tracking-wide">Low</span></td>
                <td class="px-6 py-4 text-right"><button class="text-primary font-bold text-xs hover:underline">Review</button></td>
              </tr>

              <tr class="hover:bg-primary/[0.02] transition-colors group">
                <td class="px-6 py-4"><div class="size-14 bg-slate-100 rounded border border-primary/5"></div></td>
                <td class="px-6 py-4"><div class="flex flex-col"><span class="text-sm font-bold">PAGE-6619</span><span class="text-[10px] text-slate-500 uppercase font-medium">Newspaper Archive</span></div></td>
                <td class="px-6 py-4 text-sm font-mono">JOB-22495</td>
                <td class="px-6 py-4 text-sm">invalid_border_or_crop</td>
                <td class="px-6 py-4 text-xs text-slate-500">IEP1A selected</td>
                <td class="px-6 py-4"><span class="px-2.5 py-1 rounded-full bg-red-600 text-white text-[10px] font-bold uppercase tracking-wide">Critical</span></td>
                <td class="px-6 py-4 text-right"><button class="text-primary font-bold text-xs hover:underline">Review</button></td>
              </tr>

              <tr class="hover:bg-primary/[0.02] transition-colors group border-b-0">
                <td class="px-6 py-4"><div class="size-14 bg-slate-100 rounded border border-primary/5"></div></td>
                <td class="px-6 py-4"><div class="flex flex-col"><span class="text-sm font-bold">PAGE-5508</span><span class="text-[10px] text-slate-500 uppercase font-medium">Rare Book Room</span></div></td>
                <td class="px-6 py-4 text-sm font-mono">JOB-22501</td>
                <td class="px-6 py-4 text-sm">deskew_manual_verification</td>
                <td class="px-6 py-4 text-xs text-slate-500">IEP1C selected</td>
                <td class="px-6 py-4"><span class="px-2.5 py-1 rounded-full bg-slate-100 text-slate-700 text-[10px] font-bold uppercase tracking-wide">Low</span></td>
                <td class="px-6 py-4 text-right"><button class="text-primary font-bold text-xs hover:underline">Review</button></td>
              </tr>
            </tbody>
          </table>

          <div class="bg-primary/5 border-t border-primary/10 px-6 py-4 flex items-center justify-between">
            <span class="text-xs text-slate-500">Showing 1 to 5 of 42 pages</span>
            <div class="flex items-center gap-1">
              <button class="size-8 flex items-center justify-center rounded border border-primary/10 hover:bg-primary/5 disabled:opacity-30" disabled><span class="material-symbols-outlined text-sm">chevron_left</span></button>
              <button class="size-8 flex items-center justify-center rounded bg-primary text-white text-xs font-bold">1</button>
              <button class="size-8 flex items-center justify-center rounded border border-primary/10 hover:bg-primary/5 text-xs font-bold">2</button>
              <button class="size-8 flex items-center justify-center rounded border border-primary/10 hover:bg-primary/5 text-xs font-bold">3</button>
              <span class="px-1 text-slate-400">...</span>
              <button class="size-8 flex items-center justify-center rounded border border-primary/10 hover:bg-primary/5 text-xs font-bold">9</button>
              <button class="size-8 flex items-center justify-center rounded border border-primary/10 hover:bg-primary/5"><span class="material-symbols-outlined text-sm">chevron_right</span></button>
            </div>
          </div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-4 gap-6 mt-8">
          <div class="bg-white p-5 rounded-xl border border-primary/10">
            <div class="flex items-center justify-between mb-2"><span class="text-slate-500 text-xs font-bold uppercase">Avg Fix Time</span><span class="material-symbols-outlined text-primary text-xl">timer</span></div>
            <p class="text-2xl font-bold tracking-tight">1m 24s</p>
            <p class="text-[10px] text-green-600 font-bold mt-1 flex items-center gap-1"><span class="material-symbols-outlined text-[12px]">trending_down</span>12% from yesterday</p>
          </div>
          <div class="bg-white p-5 rounded-xl border border-primary/10">
            <div class="flex items-center justify-between mb-2"><span class="text-slate-500 text-xs font-bold uppercase">Critical Flags</span><span class="material-symbols-outlined text-red-500 text-xl">error</span></div>
            <p class="text-2xl font-bold tracking-tight text-red-500">08</p>
            <p class="text-[10px] text-slate-400 font-medium mt-1">Requires immediate attention</p>
          </div>
          <div class="bg-white p-5 rounded-xl border border-primary/10">
            <div class="flex items-center justify-between mb-2"><span class="text-slate-500 text-xs font-bold uppercase">Correction Accuracy</span><span class="material-symbols-outlined text-blue-500 text-xl">check_circle</span></div>
            <p class="text-2xl font-bold tracking-tight">94.2%</p>
            <p class="text-[10px] text-blue-600 font-bold mt-1">Human-in-the-loop outcome</p>
          </div>
          <div class="bg-white p-5 rounded-xl border border-primary/10">
            <div class="flex items-center justify-between mb-2"><span class="text-slate-500 text-xs font-bold uppercase">Today's Goal</span><span class="material-symbols-outlined text-yellow-500 text-xl">flag</span></div>
            <p class="text-2xl font-bold tracking-tight">125 / 200</p>
            <div class="w-full bg-slate-100 h-1.5 rounded-full mt-3 overflow-hidden"><div class="bg-primary h-full w-[62%]"></div></div>
          </div>
        </div>
      </div>
    </main>
  </div>
</body>
</html>
```

---

## 4) `admin-correction-workspace.html`

```html
<!DOCTYPE html>
<html class="light" lang="en">
<head>
  <meta charset="utf-8" />
  <meta content="width=device-width, initial-scale=1.0" name="viewport" />
  <title>LibraryAI - Correction Workspace</title>
  <script src="https://cdn.tailwindcss.com?plugins=forms,container-queries"></script>
  <link href="https://fonts.googleapis.com/css2?family=Public+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet" />
  <link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:wght@300;400;500;600;700" rel="stylesheet" />
  <script>
    tailwind.config = {
      darkMode: "class",
      theme: {
        extend: {
          colors: {
            primary: "#840131",
            "background-light": "#f8f5f6",
            "background-dark": "#230f16",
          },
          fontFamily: {
            display: ["Public Sans", "sans-serif"],
          },
        },
      },
    };
  </script>
  <style>
    body { font-family: 'Public Sans', sans-serif; }
    .material-symbols-outlined { font-size: 20px; }
    .no-scrollbar::-webkit-scrollbar { display: none; }
  </style>
</head>
<body class="bg-background-light text-slate-900 min-h-screen flex flex-col">
  <header class="flex items-center justify-between border-b border-primary/10 bg-white px-6 py-2 shrink-0">
    <div class="flex items-center gap-6">
      <div class="flex items-center gap-2 text-primary">
        <span class="material-symbols-outlined text-3xl font-bold">tune</span>
        <h1 class="text-lg font-bold tracking-tight">Correction Workspace</h1>
      </div>
      <div class="h-6 w-px bg-slate-200"></div>
      <div class="flex items-center gap-2 text-sm text-slate-500">
        <span>Jobs</span>
        <span class="material-symbols-outlined text-xs">chevron_right</span>
        <span>JOB-22483</span>
        <span class="material-symbols-outlined text-xs">chevron_right</span>
        <span class="font-semibold text-slate-900">PAGE-9842</span>
      </div>
    </div>
    <div class="flex items-center gap-4">
      <div class="flex items-center bg-slate-100 rounded px-3 py-1.5">
        <span class="material-symbols-outlined text-slate-400 mr-2">search</span>
        <input class="bg-transparent border-none focus:ring-0 text-sm w-32 outline-none" placeholder="Jump to Page..." type="text" />
      </div>
      <button class="p-2 hover:bg-slate-100 rounded"><span class="material-symbols-outlined">notifications</span></button>
      <button class="p-2 hover:bg-slate-100 rounded"><span class="material-symbols-outlined">settings</span></button>
      <div class="h-8 w-8 rounded-full bg-primary/20 flex items-center justify-center border border-primary/30">
        <span class="text-primary font-bold text-xs">AD</span>
      </div>
    </div>
  </header>

  <main class="flex flex-1 overflow-hidden">
    <aside class="w-64 border-r border-slate-200 bg-white flex flex-col shrink-0">
      <div class="p-4 border-b border-slate-100">
        <h3 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-3">Source Images</h3>
        <nav class="space-y-1">
          <button class="w-full flex items-center gap-2 px-3 py-2 rounded bg-primary text-white text-xs font-semibold shadow-sm">
            <span class="material-symbols-outlined !text-[18px]">raw_on</span>
            Original OTIFF
          </button>
          <button class="w-full flex items-center gap-2 px-3 py-2 rounded text-slate-600 hover:bg-slate-100 text-xs font-semibold">
            <span class="material-symbols-outlined !text-[18px]">verified</span>
            Best Available Output
          </button>
        </nav>
      </div>

      <div class="p-4 flex-1 overflow-y-auto">
        <h3 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-3">Branch Outputs</h3>
        <div class="grid grid-cols-1 gap-2">
          <button class="flex flex-col gap-1 p-3 border border-slate-200 rounded-lg text-left hover:border-primary/50 transition-colors group">
            <div class="flex justify-between items-center">
              <span class="text-sm font-bold group-hover:text-primary">IEP1A</span>
              <span class="text-[10px] px-1.5 py-0.5 rounded bg-green-100 text-green-700 font-bold uppercase">Pass</span>
            </div>
            <span class="text-[11px] text-slate-500">Contour / fast heuristic</span>
          </button>

          <button class="flex flex-col gap-1 p-3 border border-slate-200 rounded-lg text-left hover:border-primary/50 transition-colors group">
            <div class="flex justify-between items-center">
              <span class="text-sm font-bold group-hover:text-primary">IEP1B</span>
              <span class="text-[10px] px-1.5 py-0.5 rounded bg-amber-100 text-amber-700 font-bold uppercase">Warn</span>
            </div>
            <span class="text-[11px] text-slate-500">Component based branch</span>
          </button>

          <button class="flex flex-col gap-1 p-3 border-2 border-primary/20 bg-primary/5 rounded-lg text-left">
            <div class="flex justify-between items-center">
              <span class="text-sm font-bold text-primary">IEP1C</span>
              <span class="text-[10px] px-1.5 py-0.5 rounded bg-primary text-white font-bold uppercase">Selected</span>
            </div>
            <span class="text-[11px] text-slate-500">Validation branch</span>
          </button>

          <button class="flex flex-col gap-1 p-3 border border-slate-200 rounded-lg text-left opacity-60">
            <div class="flex justify-between items-center">
              <span class="text-sm font-bold">IEP1D</span>
              <span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 font-bold uppercase">Optional</span>
            </div>
            <span class="text-[11px] text-slate-500">Learned preprocessing</span>
          </button>
        </div>
      </div>

      <div class="p-4 border-t border-slate-100">
        <div class="p-3 bg-slate-50 rounded-lg border border-slate-200">
          <p class="text-[11px] text-slate-500 mb-2">Review Reason</p>
          <p class="text-xs font-medium text-slate-800">preprocessing_consensus_failed</p>
        </div>
      </div>
    </aside>

    <section class="flex-1 bg-slate-100 flex flex-col relative overflow-hidden">
      <div class="h-12 bg-white border-b border-slate-200 flex items-center justify-between px-4 shrink-0">
        <div class="flex items-center gap-1">
          <button class="p-2 hover:bg-slate-100 rounded transition-colors" title="Zoom In"><span class="material-symbols-outlined">zoom_in</span></button>
          <button class="p-2 hover:bg-slate-100 rounded transition-colors" title="Zoom Out"><span class="material-symbols-outlined">zoom_out</span></button>
          <div class="h-6 w-px bg-slate-200 mx-1"></div>
          <button class="p-2 hover:bg-slate-100 rounded transition-colors" title="Rotate Left"><span class="material-symbols-outlined">rotate_left</span></button>
          <button class="p-2 hover:bg-slate-100 rounded transition-colors" title="Rotate Right"><span class="material-symbols-outlined">rotate_right</span></button>
          <div class="h-6 w-px bg-slate-200 mx-1"></div>
          <button class="p-2 bg-primary/10 text-primary rounded" title="Crop Tool"><span class="material-symbols-outlined">crop</span></button>
          <button class="p-2 hover:bg-slate-100 rounded" title="Split Line"><span class="material-symbols-outlined">splitscreen</span></button>
          <div class="h-6 w-px bg-slate-200 mx-1"></div>
          <button class="flex items-center gap-2 px-3 py-1.5 border border-slate-200 rounded text-xs font-bold hover:bg-slate-50">
            <span class="material-symbols-outlined !text-[18px]">compare</span>
            Compare Mode
          </button>
        </div>

        <div class="flex items-center gap-3">
          <div class="flex items-center gap-2">
            <span class="text-[10px] font-bold text-slate-400 uppercase">Deskew</span>
            <input class="w-24 accent-primary h-1 bg-slate-200 rounded-lg appearance-none cursor-pointer" max="100" min="0" type="range" value="42" />
            <div class="flex items-center bg-slate-100 rounded px-2 py-0.5 border border-slate-200">
              <input class="bg-transparent border-none p-0 focus:ring-0 text-[11px] font-mono w-8 text-center" type="text" value="0.42" />
              <span class="text-[10px] text-slate-400">°</span>
            </div>
          </div>
        </div>
      </div>

      <div class="flex-1 p-8 flex items-center justify-center relative overflow-auto">
        <div class="relative bg-white shadow-2xl transition-transform" style="width: 500px; height: 700px; transform: rotate(0.42deg);">
          <div class="absolute inset-0 p-12 overflow-hidden bg-white/10" style="background-image: linear-gradient(#f0f0f0 1px, transparent 1px), linear-gradient(90deg, #f0f0f0 1px, transparent 1px); background-size: 20px 20px;">
            <div class="w-full h-8 bg-slate-200 mb-6 rounded"></div>
            <div class="space-y-4">
              <div class="w-full h-4 bg-slate-100 rounded"></div>
              <div class="w-[90%] h-4 bg-slate-100 rounded"></div>
              <div class="w-full h-4 bg-slate-100 rounded"></div>
              <div class="w-full h-4 bg-slate-100 rounded"></div>
              <div class="w-[85%] h-4 bg-slate-100 rounded"></div>
              <div class="w-full h-4 bg-slate-100 rounded"></div>
              <div class="w-[95%] h-4 bg-slate-100 rounded"></div>
              <div class="w-full h-4 bg-slate-100 rounded"></div>
            </div>
            <div class="mt-12 w-48 h-48 bg-slate-200/50 rounded flex items-center justify-center">
              <span class="material-symbols-outlined text-slate-300 text-6xl">image</span>
            </div>
          </div>

          <div class="absolute inset-8 border-2 border-dashed border-primary pointer-events-none">
            <div class="absolute -top-1.5 -left-1.5 w-3 h-3 bg-primary rounded-full cursor-nw-resize pointer-events-auto"></div>
            <div class="absolute -top-1.5 -right-1.5 w-3 h-3 bg-primary rounded-full cursor-ne-resize pointer-events-auto"></div>
            <div class="absolute -bottom-1.5 -left-1.5 w-3 h-3 bg-primary rounded-full cursor-sw-resize pointer-events-auto"></div>
            <div class="absolute -bottom-1.5 -right-1.5 w-3 h-3 bg-primary rounded-full cursor-se-resize pointer-events-auto"></div>
          </div>

          <div class="absolute top-0 bottom-0 left-1/2 w-0.5 bg-blue-500/50 cursor-col-resize flex items-center justify-center">
            <div class="h-8 w-1.5 bg-blue-500 rounded-full"></div>
          </div>
        </div>

        <div class="absolute bottom-6 right-6 bg-slate-900/80 backdrop-blur-sm text-white px-3 py-1.5 rounded-full text-xs font-bold border border-white/10">
          100% Zoom
        </div>
      </div>
    </section>

    <aside class="w-80 border-l border-slate-200 bg-white flex flex-col shrink-0 overflow-y-auto">
      <div class="p-4 border-b border-slate-100">
        <div class="flex items-center justify-between mb-3">
          <div class="flex items-center gap-2">
            <span class="material-symbols-outlined text-primary !text-[18px]">info</span>
            <h3 class="text-xs font-bold uppercase tracking-tight">Page Metadata</h3>
          </div>
        </div>
        <div class="grid grid-cols-1 gap-2">
          <div class="flex items-center justify-between text-[11px]"><span class="text-slate-500">Format</span><span class="font-mono bg-slate-50 px-1.5 py-0.5 rounded">OTIFF</span></div>
          <div class="flex items-center justify-between text-[11px]"><span class="text-slate-500">Size</span><span class="font-mono bg-slate-50 px-1.5 py-0.5 rounded">2480 × 3508</span></div>
          <div class="flex items-center justify-between text-[11px]"><span class="text-slate-500">DPI</span><span class="font-mono bg-slate-50 px-1.5 py-0.5 rounded">300</span></div>
          <div class="flex items-center justify-between text-[11px]"><span class="text-slate-500">Material Type</span><span class="font-mono bg-slate-50 px-1.5 py-0.5 rounded">manuscript</span></div>
        </div>
      </div>

      <div class="p-4 border-b border-slate-100">
        <div class="flex items-center justify-between mb-4">
          <h3 class="text-xs font-bold uppercase tracking-tight">Corrections</h3>
          <button class="text-[10px] font-bold text-primary uppercase hover:underline flex items-center gap-1">
            <span class="material-symbols-outlined !text-[14px]">bolt</span>
            Auto-populate
          </button>
        </div>

        <div class="space-y-3">
          <div class="grid grid-cols-2 gap-2">
            <div>
              <label class="block text-[10px] font-bold text-slate-400 mb-1 uppercase">Crop X Min</label>
              <input class="w-full bg-white border-slate-200 rounded text-xs py-1 px-2 focus:ring-primary focus:border-primary" type="number" value="40" />
            </div>
            <div>
              <label class="block text-[10px] font-bold text-slate-400 mb-1 uppercase">Crop Y Min</label>
              <input class="w-full bg-white border-slate-200 rounded text-xs py-1 px-2 focus:ring-primary focus:border-primary" type="number" value="32" />
            </div>
          </div>

          <div class="grid grid-cols-2 gap-2">
            <div>
              <label class="block text-[10px] font-bold text-slate-400 mb-1 uppercase">Crop X Max</label>
              <input class="w-full bg-white border-slate-200 rounded text-xs py-1 px-2 focus:ring-primary focus:border-primary" type="number" value="2440" />
            </div>
            <div>
              <label class="block text-[10px] font-bold text-slate-400 mb-1 uppercase">Crop Y Max</label>
              <input class="w-full bg-white border-slate-200 rounded text-xs py-1 px-2 focus:ring-primary focus:border-primary" type="number" value="3440" />
            </div>
          </div>

          <div>
            <label class="block text-[10px] font-bold text-slate-400 mb-1 uppercase">Deskew Angle (°)</label>
            <input class="w-full bg-white border-slate-200 rounded text-xs py-1 px-2 focus:ring-primary focus:border-primary" type="text" value="0.42" />
          </div>

          <div>
            <label class="block text-[10px] font-bold text-slate-400 mb-1 uppercase">Split X (Optional)</label>
            <input class="w-full bg-white border-slate-200 rounded text-xs py-1 px-2 focus:ring-primary focus:border-primary" type="number" value="1240" />
          </div>
        </div>
      </div>

      <div class="p-4 flex-1">
        <div class="flex items-center gap-2 mb-2">
          <span class="material-symbols-outlined text-slate-400 !text-[18px]">rate_review</span>
          <h3 class="text-xs font-bold uppercase tracking-tight">Reviewer Notes</h3>
        </div>
        <textarea class="w-full h-28 bg-slate-50 border-slate-200 rounded text-[11px] p-2.5 focus:ring-primary focus:border-primary resize-none" placeholder="Add notes for this correction..."></textarea>
        <div class="mt-3 p-2 bg-blue-50 border border-blue-100 rounded flex gap-2">
          <span class="material-symbols-outlined text-blue-500 !text-sm mt-0.5">lightbulb</span>
          <p class="text-[10px] text-blue-800 leading-normal">Suggestion: border detection appears too tight on the bottom margin.</p>
        </div>
      </div>

      <div class="p-4 bg-slate-50 border-t border-slate-200 space-y-2">
        <button class="w-full py-3 bg-primary text-white font-bold rounded text-xs hover:brightness-110 transition-all flex items-center justify-center gap-2 shadow-lg shadow-primary/20">
          <span class="material-symbols-outlined">check_circle</span>
          Submit Correction
        </button>
        <button class="w-full py-2.5 border border-slate-200 text-slate-500 font-bold rounded text-xs hover:bg-red-50 hover:text-red-600 hover:border-red-200 transition-all flex items-center justify-center gap-2">
          <span class="material-symbols-outlined">cancel</span>
          Reject Page
        </button>
      </div>
    </aside>
  </main>

  <footer class="h-8 bg-white border-t border-slate-200 flex items-center justify-between px-6 text-[10px] uppercase font-bold tracking-widest text-slate-500 shrink-0">
    <div class="flex items-center gap-4">
      <span class="flex items-center gap-1"><span class="w-2 h-2 rounded-full bg-green-500"></span> Workspace Connected</span>
      <span class="text-slate-300">|</span>
      <span>Queue Progress: 12 / 42 Pages</span>
    </div>
    <div class="flex items-center gap-4">
      <span>Last Sync: 2m ago</span>
      <span class="text-primary hover:underline cursor-pointer">Keyboard Shortcuts [?]</span>
    </div>
  </footer>
</body>
</html>
```
