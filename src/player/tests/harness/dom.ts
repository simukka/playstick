// A headless stand-in for the slice of the DOM the views touch.
//
// Same philosophy as the old tests/js harness: no jsdom. The views under test
// do a bounded set of things -- create elements, set text and a few properties,
// toggle classes, append/insert/remove children, attach click listeners -- and a
// hand-rolled stub of exactly that is faster to start, easy to assert against,
// and records the operations a real DOM would only perform. Cast to Document /
// HTMLElement at the boundary; production code uses the real lib.dom types.

export class FakeClassList {
  private set = new Set<string>();
  add(...cs: string[]): void {
    for (const c of cs) this.set.add(c);
  }
  remove(...cs: string[]): void {
    for (const c of cs) this.set.delete(c);
  }
  toggle(c: string, on?: boolean): boolean {
    const next = on ?? !this.set.has(c);
    if (next) this.set.add(c);
    else this.set.delete(c);
    return next;
  }
  contains(c: string): boolean {
    return this.set.has(c);
  }
  get value(): string {
    return [...this.set].join(" ");
  }
}

export class FakeEl {
  readonly tagName: string;
  children: FakeEl[] = [];
  parent: FakeEl | null = null;
  private text = "";
  private handlers = new Map<string, Array<(ev: unknown) => void>>();
  private attrs = new Map<string, string>();
  readonly classList = new FakeClassList();
  readonly style: Record<string, string> = {};
  dataset: Record<string, string> = {};

  // The props the views actually assign. Kept plain so a test can read them back.
  src = "";
  alt = "";
  loading = "";
  type = "";
  value = "";
  hidden = false;
  disabled = false;
  innerHTML = "";
  ariaLabel = "";

  // Counters, so a benchmark and the churn tests can see how much the reconciler
  // actually moved rather than trusting the final tree.
  inserts = 0;
  removes = 0;
  creates = 0;

  // The views build their markup rather than looking it up, so an id is set
  // here on the way past. The document it came from indexes it, which is what
  // makes getElementById resolve the page the views actually built -- and what
  // lets a test go on asking for an element by the id the browser would use.
  private _id = "";
  constructor(
    tagName: string,
    private readonly doc?: FakeDocument,
  ) {
    this.tagName = tagName.toLowerCase();
  }

  get id(): string {
    return this._id;
  }
  set id(v: string) {
    this._id = v;
    this.doc?.index(this);
  }

  get textContent(): string {
    return this.text;
  }
  // Assigning textContent replaces everything under the node, which is how a
  // list is cleared before a repaint. A stub that only stored the string would
  // hide a child leak.
  set textContent(v: string) {
    this.text = v;
    this.children = [];
  }

  get className(): string {
    return this.classList.value;
  }
  set className(v: string) {
    this.classList.remove(...this.classList.value.split(/\s+/).filter(Boolean));
    this.classList.add(...String(v).split(/\s+/).filter(Boolean));
  }

  get firstChild(): FakeEl | null {
    return this.children[0] ?? null;
  }
  get childNodes(): FakeEl[] {
    return this.children;
  }

  appendChild(child: FakeEl): FakeEl {
    child.remove();
    child.parent = this;
    this.children.push(child);
    this.inserts++;
    return child;
  }

  insertBefore(child: FakeEl, ref: FakeEl | null): FakeEl {
    child.remove();
    child.parent = this;
    const idx = ref ? this.children.indexOf(ref) : -1;
    if (idx < 0) this.children.push(child);
    else this.children.splice(idx, 0, child);
    this.inserts++;
    return child;
  }

  removeChild(child: FakeEl): FakeEl {
    const idx = this.children.indexOf(child);
    if (idx >= 0) {
      this.children.splice(idx, 1);
      child.parent = null;
      this.removes++;
    }
    return child;
  }

  remove(): void {
    this.parent?.removeChild(this);
  }

  setAttribute(k: string, v: string): void {
    this.attrs.set(k, v);
    if (k === "aria-label") this.ariaLabel = v;
  }
  getAttribute(k: string): string | null {
    return this.attrs.get(k) ?? null;
  }

  addEventListener(type: string, fn: (ev: unknown) => void): void {
    const list = this.handlers.get(type) ?? [];
    list.push(fn);
    this.handlers.set(type, list);
  }
  removeEventListener(): void {}

  // Drive a handler the way a tap would.
  fire(type: string): void {
    for (const fn of this.handlers.get(type) ?? []) {
      fn({ preventDefault() {} });
    }
  }
  click(): void {
    this.fire("click");
  }

  // Depth-first search by id, for asserting against the built tree.
  find(id: string): FakeEl | null {
    if (this.id === id) return this;
    for (const c of this.children) {
      const hit = c.find(id);
      if (hit) return hit;
    }
    return null;
  }
}

export class FakeDocument {
  private byId = new Map<string, FakeEl>();
  readonly head = new FakeEl("head");
  readonly body = new FakeEl("body");
  readonly documentElement = new FakeEl("html");
  creates = 0;

  /** Called by an element when it is given an id. */
  index(el: FakeEl): void {
    this.byId.set(el.id, el);
  }

  getElementById(id: string): FakeEl | null {
    return this.byId.get(id) ?? null;
  }

  createElement(tag: string): FakeEl {
    this.creates++;
    const el = new FakeEl(tag, this);
    el.creates++;
    return el;
  }
}

/** Cast helpers: the stub covers what the views use, not all of lib.dom. */
export function asDocument(d: FakeDocument): Document {
  return d as unknown as Document;
}
export function asEl(e: FakeEl): HTMLElement {
  return e as unknown as HTMLElement;
}
