const sheet={busy:false};const output=document.querySelector('#result');document.querySelector('#calculate').addEventListener('click',async()=>{if(sheet.busy)return;sheet.busy=true;output.textContent='计算中…';try{const dataset=document.querySelector('#dataset').value;const response=await fetch(dataset===document.body.dataset.source?'/api/runs/'+document.body.dataset.runId+'/rerun':'/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dataset})});const data=await response.json();if(!response.ok)throw Error(data.detail);const url=new URL(location.href);url.searchParams.set('dataset',dataset);url.searchParams.set('run_id',data.manifest.run_id);location.href=url;}catch(e){output.textContent=e.message;}finally{sheet.busy=false;}});document.querySelector('#member-filter').addEventListener('input',e=>document.querySelectorAll('.primary-panel tbody tr').forEach(row=>row.hidden=!row.textContent.includes(e.target.value)));document.querySelectorAll('td[tabindex]').forEach(cell=>cell.addEventListener('focus',()=>document.querySelector('#selection').textContent=cell.textContent));document.querySelector('#dataset').value=new URL(location.href).searchParams.get('dataset')||document.querySelector('#dataset').value;
const filePicker=document.querySelector('#csv-file');
if(filePicker){
 const editor=document.querySelector('#csv-text');
 filePicker.addEventListener('change',async()=>{const file=filePicker.files[0];if(!file)return;if(file.size>2000000){editor.value='';alert('文件超过2MB，请缩小后导入');return;}const bytes=await file.arrayBuffer();try{editor.value=new TextDecoder('utf-8',{fatal:true}).decode(bytes);}catch(e){editor.value=new TextDecoder('gb18030',{fatal:true}).decode(bytes);}});
 document.querySelector('#import-csv').addEventListener('click',async event=>{event.target.disabled=true;try{const response=await fetch('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csv_text:editor.value})});const data=await response.json();if(!response.ok)throw Error(data.detail);location.href=location.pathname+'?run_id='+data.manifest.run_id;}catch(e){editor.setCustomValidity(e.message);editor.reportValidity();}finally{event.target.disabled=false;}});
 editor.addEventListener('input',()=>editor.setCustomValidity(''));
}
const publishBtn=document.querySelector('#publish-btn');
if(publishBtn){
 const publishOutput=document.querySelector('#publish-result');
 const runId=document.body.dataset.runId;
 publishBtn.addEventListener('click',async()=>{
  publishBtn.disabled=true;publishOutput.textContent='发布中…';
  try{
   const response=await fetch('/api/runs/'+runId+'/publish',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:Number(publishBtn.dataset.revision)})});
   const data=await response.json();
   if(!response.ok)throw Error(data.detail);
   publishOutput.textContent=data.replayed===false?'已发布（幂等重试，未重复恢复）':'发布成功：'+data.run_id+' r'+data.revision;
   location.reload();
  }catch(e){publishOutput.textContent='发布失败，旧版结果保留：'+e.message;}
  finally{publishBtn.disabled=false;}
 });
 const withdrawBtn=document.querySelector('#withdraw-btn');
 withdrawBtn.addEventListener('click',async()=>{
  withdrawBtn.disabled=true;publishOutput.textContent='撤回中…';
  try{
   const response=await fetch('/api/runs/'+runId+'/publish',{method:'DELETE'});
   const data=await response.json();
   if(!response.ok)throw Error(data.detail);
   location.reload();
  }catch(e){publishOutput.textContent=e.message;}
  finally{withdrawBtn.disabled=false;}
 });
}
