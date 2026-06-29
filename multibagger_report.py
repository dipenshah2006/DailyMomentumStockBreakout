#!/usr/bin/env python3
  <th onclick="thSort(9)">Fib 1.618x</th>
  <th onclick="thSort(10)">Fib 2.618x</th>
  <th onclick="thSort(11)">Fib 4.236x</th>
  <th onclick="thSort(12)" style="text-align:left">Signal</th>
  <th onclick="thSort(13)">Score</th>
  <th onclick="thSort(14)" style="color:#ff8c00">💥 Blast</th>
  <th onclick="thSort(15)">Trades</th>
  <th onclick="thSort(16)">Win Rate</th>
  <th onclick="thSort(17)">Avg Ret</th>
  <th onclick="thSort(18)">Best Ret</th>
  <th style="text-align:left">Charts</th>
</tr>
</thead>
<tbody id="tableBody">
{rows_html}
</tbody>
</table>

<div class="footer">
  Multibagger Report v1.0 &nbsp;|&nbsp; {run_ts} &nbsp;|&nbsp;
  <b>Not financial advice.</b> For educational purposes only.<br>
  Strategy: ATH Breakout + M-RSI&gt;70 + Ultra-Slow MACD(34,1000,20) + Fibonacci Extensions
</div>

<script>
function toggleDetail(id){{
  const el=document.getElementById(id);
  el.style.display=el.style.display==='none'?'table-row':'none';
}}
function filterTable(){{
  const q=document.getElementById('searchInput').value.toLowerCase();
  const rows=document.querySelectorAll('#tableBody tr[data-name]');
  let shown=0;
  rows.forEach(r=>{{
    const match=r.dataset.name.toLowerCase().includes(q)||
                r.querySelector('b').textContent.toLowerCase().includes(q);
    r.style.display=match?'':'none';
    const nextId=r.querySelector('button')?.getAttribute('onclick')?.match(/'(d\\d+)'/)?.[1];
    if(nextId){{document.getElementById(nextId).style.display='none';}}
    if(match)shown++;
  }});
  document.getElementById('countInfo').textContent=`Showing ${{shown}} of {n_total}`;
}}
function sortBy(field){{
  const rows=[...document.querySelectorAll('#tableBody tr[data-name]')];
  const map={{score:'score',mrsi:'mrsi',wrsi:'wrsi',drsi:'drsi',ath:'ath'}};
  rows.sort((a,b)=>parseFloat(b.dataset[map[field]]||0)-parseFloat(a.dataset[map[field]]||0));
  const tbody=document.getElementById('tableBody');
  rows.forEach(r=>{{tbody.appendChild(r);}});
  document.querySelectorAll('.sort-btn').forEach(b=>b.classList.remove('active'));
  event.target.classList.add('active');
}}
let lastThSort=-1, lastThDir=1;
function thSort(col){{
  const tbody=document.getElementById('tableBody');
  const rows=[...tbody.querySelectorAll('tr[data-name]')];
  const dir=lastThSort===col?-lastThDir:1;
  lastThSort=col; lastThDir=dir;
  rows.sort((a,b)=>{{
    const av=a.cells[col]?.textContent.replace(/[₹,%+▲▼ ]/g,'').trim()||'';
    const bv=b.cells[col]?.textContent.replace(/[₹,%+▲▼ ]/g,'').trim()||'';
    const an=parseFloat(av), bn=parseFloat(bv);
    if(!isNaN(an)&&!isNaN(bn)) return dir*(an-bn);
    return dir*av.localeCompare(bv);
  }});
  rows.forEach(r=>tbody.appendChild(r));
  document.querySelectorAll('th').forEach((h,i)=>{{
    h.className=i===col?(dir===1?'asc':'desc'):'';
  }});
}}
document.addEventListener('DOMContentLoaded',()=>{{
  document.getElementById('countInfo').textContent=`Showing {n_total} stocks`;
}});
</script>
</body>
</html>'''


def _trade_table(trades):
    if not trades:
        return '<p style="color:#8b949e;font-size:12px;margin-top:12px">No completed backtest trades found.</p>'
    rows = []
    for t in trades[-10:]:  # last 10 trades
        rc = '#26d07c' if t['return_pct'] > 0 else '#ff6b6b'
