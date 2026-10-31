"""Le variabili Tk (StringVar...) delle finestre chiuse vengono liberate da Python "quando
capita", anche in un thread secondario (sync, login, verifica). Lì il loro distruttore non deve
bloccare aspettando il thread principale."""
import gc
import threading
import time

import pytest

tk = pytest.importorskip("tkinter")


def _display_ok():
    try:
        tk.Tk().destroy()
        return True
    except tk.TclError:
        return False


pytestmark = pytest.mark.skipif(not _display_ok(), reason="nessun display")


@pytest.fixture(autouse=True)
def _free_tk_in_main_thread():
    """Dopo che pytest ha lasciato la finestra: la si libera qui, nel thread principale.
    Un interprete Tcl liberato dal garbage collector in un altro thread manda in crash il
    processo (l'app vera ha una sola finestra radice per tutta la vita, i test tante)."""
    yield
    import gc
    gc.collect()


@pytest.fixture
def root():
    """La finestra radice si chiude E si libera nel thread principale: un interprete Tcl
    liberato da un altro thread manda in crash il processo (nei test ne creiamo tanti, l'app
    vera ne ha uno solo per tutta la vita)."""
    gc.collect()  # la spazzatura lasciata dai test precedenti si raccoglie qui, non nel thread
    r = tk.Tk()
    yield r
    try:
        r.destroy()
    except tk.TclError:
        pass
    del r
    gc.collect()


def test_tk_variables_freed_in_background_thread_do_not_block(root):
    from resosync import tkfix
    tkfix.install(root, every_ms=20)  # come fa la finestra di ResoSync
    holder = []
    for i in range(10):
        v = tk.StringVar(root, value=f"x{i}")
        v.cycle = v  # come in una finestra chiusa: si libera solo col garbage collector
        holder.append(v)
    names = [v._name for v in holder]
    del holder, v
    took = []

    def worker():
        t = time.time()
        gc.collect()  # il garbage collector parte in questo thread
        took.append(time.time() - t)
    th = threading.Thread(target=worker)
    th.start()
    end = time.time() + 30
    while th.is_alive() and time.time() < end:
        root.update()  # il thread principale non è in mainloop (come nei test)
        time.sleep(0.01)
    th.join(1)
    assert took and took[0] < 1.0, f"il thread è rimasto bloccato {took}"
    for _ in range(5):
        root.update()
        time.sleep(0.05)
    # e le variabili spariscono davvero da Tcl (nel thread principale)
    assert not [n for n in names if root.getboolean(root.call("info", "exists", n))]


def test_quit_waiting_for_sync_does_not_deadlock(root):
    """Come alla chiusura di ResoSync: il thread principale (dentro mainloop) aspetta che la
    sync finisca, e intanto il thread della sync libera delle variabili Tk. Prima il thread
    della sync aspettava il principale e il principale la sync: tutto fermo fino al timeout."""
    from resosync import tkfix
    tkfix.install(root, every_ms=20)
    lock = threading.Lock()
    lock.acquire()  # la "sync" in corso tiene il lock
    holder = []
    for i in range(5):
        v = tk.StringVar(root, value=str(i))
        v.cycle = v
        holder.append(v)
    del holder, v
    result = {}

    def sync_thread():
        gc.collect()  # la sync alloca e il garbage collector parte qui
        lock.release()

    def quit_like():
        threading.Thread(target=sync_thread, daemon=True).start()
        t = time.time()
        result["got"] = lock.acquire(timeout=8)  # come SyncService.stop(wait=...)
        result["took"] = time.time() - t
        root.quit()
    root.after(50, quit_like)
    root.after(15000, root.quit)
    root.mainloop()
    assert result.get("got") and result["took"] < 2, f"bloccato: {result}"
