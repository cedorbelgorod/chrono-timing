/* Автоподстановка данных участника из общего списка ранее регистрировавшихся. */
(function () {
    if (window.PeopleAC) return;

    var style = document.createElement('style');
    style.textContent =
        '.pac-box{position:fixed;z-index:9999;background:#fff;border:1px solid #d1d5db;border-radius:10px;' +
        'box-shadow:0 12px 32px rgba(0,0,0,.16);max-height:290px;overflow:auto;display:none;padding:4px;' +
        'font-family:-apple-system,"Segoe UI",Roboto,sans-serif}' +
        '.pac-item{padding:9px 12px;border-radius:7px;cursor:pointer;font-size:14px;line-height:1.3}' +
        '.pac-item:hover,.pac-item.on{background:#eff6ff}' +
        '.pac-name{font-weight:600;color:#111827}' +
        '.pac-meta{color:#6b7280;font-size:12px;margin-top:2px}';
    document.head.appendChild(style);

    function resolve(x) {
        if (typeof x === 'function') return x();
        if (typeof x === 'string') return document.querySelector(x);
        return x || null;
    }

    function fire(el) {
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function attach(input, opts) {
        if (!input) return;
        opts = opts || {};

        var box = document.createElement('div');
        box.className = 'pac-box';
        document.body.appendChild(box);

        var items = [];
        var active = -1;
        var timer = null;
        var seq = 0;
        var picked = false;

        function position() {
            var r = input.getBoundingClientRect();
            var h = box.offsetHeight || 0;
            var width = Math.max(r.width, 280);
            var left = Math.min(r.left, window.innerWidth - width - 6);
            if (left < 6) left = 6;

            var spaceBelow = window.innerHeight - r.bottom - 8;
            var top = r.bottom + 4;
            // если снизу не помещается — показываем над полем (важно, когда открыта клавиатура)
            if (h && h > spaceBelow && r.top > h + 8) {
                top = r.top - h - 4;
            }
            box.style.left = left + 'px';
            box.style.top = top + 'px';
            box.style.width = width + 'px';
        }

        function hide() {
            box.style.display = 'none';
            box.innerHTML = '';
            items = [];
            active = -1;
        }

        function fill(p) {
            input.value = p.full_name || '';
            var year = resolve(opts.year);
            if (year && p.birth_year) { year.value = String(p.birth_year); fire(year); }
            var city = resolve(opts.city);
            if (city && p.city) { city.value = p.city; fire(city); }
            var team = resolve(opts.team);
            if (team && p.team) { team.value = p.team; fire(team); }
            var gender = resolve(opts.gender);
            if (gender && p.gender) { gender.value = p.gender; fire(gender); }
            fire(input);
            hide();
            if (typeof opts.onPick === 'function') {
                try { opts.onPick(p); } catch (e) {}
            }
        }

        function pick(p) {
            if (picked) return;
            picked = true;
            fill(p);
        }

        function setActive(i) {
            if (!items.length) return;
            active = (i + items.length) % items.length;
            Array.prototype.forEach.call(box.children, function (el, n) {
                el.classList.toggle('on', n === active);
            });
            if (box.children[active]) box.children[active].scrollIntoView({ block: 'nearest' });
        }

        function render(list) {
            items = list;
            active = -1;
            picked = false;
            box.innerHTML = '';
            list.forEach(function (p, i) {
                var it = document.createElement('div');
                it.className = 'pac-item';

                var name = document.createElement('div');
                name.className = 'pac-name';
                name.textContent = p.full_name;

                var genderText = p.gender === 'M' ? 'М' : (p.gender === 'F' ? 'Ж' : '');
                var metaText = [p.birth_year ? p.birth_year + ' г.' : '', genderText, p.city || '', p.team || '']
                    .filter(Boolean).join(' · ');
                it.appendChild(name);
                if (metaText) {
                    var meta = document.createElement('div');
                    meta.className = 'pac-meta';
                    meta.textContent = metaText;
                    it.appendChild(meta);
                }

                it.dataset.i = String(i);
                // мышь: не даём полю потерять фокус, выбор — по клику
                it.addEventListener('mousedown', function (e) { e.preventDefault(); });
                it.addEventListener('click', function (e) {
                    e.preventDefault();
                    // после прокрутки случайный клик не считаем выбором
                    if (Date.now() - lastScrollAt < 500) return;
                    pick(p);
                });
                it.addEventListener('mouseenter', function () { setActive(i); });
                box.appendChild(it);
            });
            box.style.display = 'block';
            position();
        }

        /* --- касания: выбираем только короткий тап без прокрутки --- */
        var touchStart = null;
        var touchMoved = false;
        var touching = false;
        var lastScrollAt = 0;

        box.addEventListener('touchstart', function (e) {
            var t = e.touches[0];
            touchStart = {
                x: t.clientX, y: t.clientY, time: Date.now(),
                scroll: box.scrollTop, target: e.target
            };
            touchMoved = false;
            touching = true;
        }, { passive: true });

        box.addEventListener('touchmove', function (e) {
            if (!touchStart) return;
            var t = e.touches[0];
            if (Math.abs(t.clientX - touchStart.x) > 10 || Math.abs(t.clientY - touchStart.y) > 10) {
                touchMoved = true;
                lastScrollAt = Date.now();
            }
        }, { passive: true });

        box.addEventListener('touchend', function (e) {
            var start = touchStart;
            touchStart = null;
            setTimeout(function () { touching = false; }, 250);
            if (!start) return;
            if (touchMoved) return;                            // это была прокрутка
            if (Date.now() - start.time > 800) return;         // долгое удержание
            if (box.scrollTop !== start.scroll) return;        // список успели прокрутить
            var item = start.target && start.target.closest ? start.target.closest('.pac-item') : null;
            if (!item) return;
            var person = items[parseInt(item.dataset.i, 10)];
            if (!person) return;
            e.preventDefault();                                // отменяем синтетический клик
            pick(person);
        }, { passive: false });

        box.addEventListener('touchcancel', function () {
            touchStart = null;
            touching = false;
        }, { passive: true });

        function search(q) {
            var my = ++seq;
            fetch('/api/people?q=' + encodeURIComponent(q), { cache: 'no-store' })
                .then(function (r) { return r.ok ? r.json() : { items: [] }; })
                .then(function (data) {
                    if (my !== seq) return;
                    var list = data.items || [];
                    // показываем только когда вариантов меньше пяти
                    if (q.trim().length < 2 || list.length === 0 || list.length >= 5) { hide(); return; }
                    render(list);
                })
                .catch(function () { hide(); });
        }

        input.setAttribute('autocomplete', 'off');
        input.addEventListener('input', function () {
            clearTimeout(timer);
            var q = input.value.trim();
            if (q.length < 2) { hide(); return; }
            timer = setTimeout(function () { search(q); }, 220);
        });

        input.addEventListener('keydown', function (e) {
            if (box.style.display !== 'block') return;
            if (e.key === 'ArrowDown') { e.preventDefault(); setActive(active + 1); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(active - 1); }
            else if (e.key === 'Enter') {
                if (active >= 0 && items[active]) { e.preventDefault(); fill(items[active]); }
            } else if (e.key === 'Escape') { hide(); }
        });

        input.addEventListener('blur', function () {
            setTimeout(function () { if (!touching) hide(); }, 200);
        });
        window.addEventListener('scroll', function () {
            if (box.style.display === 'block') position();
        }, true);
        window.addEventListener('resize', function () {
            if (box.style.display === 'block') position();
        });
    }

    window.PeopleAC = { attach: attach };
})();
