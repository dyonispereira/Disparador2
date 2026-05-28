const API = "http://" + window.location.hostname + ":8000";

// STATUS
async function loadStatus(){
  const r = await fetch(API+"/status");
  const d = await r.json();

  document.getElementById("status").innerText = d.state;
}

// QR
async function loadQR(){
  const r = await fetch(API+"/qr");
  const d = await r.json();

  document.getElementById("qr").innerText = JSON.stringify(d);
}

// LEADS
async function uploadLeads(){
  const file = document.getElementById("leadsFile").files[0];

  const fd = new FormData();
  fd.append("file", file);

  await fetch(API+"/upload-leads",{method:"POST",body:fd});
  loadLeads();
}

async function loadLeads(){
  const r = await fetch(API+"/leads");
  const d = await r.json();

  let html = "";
  d.forEach(l=>{
    html += `<tr><td>${l.nome}</td><td>${l.telefone}</td><td>${l.status}</td></tr>`;
  });

  document.getElementById("leadsTable").innerHTML = html;
}

// MENSAGENS
async function saveMsg(){
  const texto = document.getElementById("msg").value;

  const fd = new FormData();
  fd.append("texto", texto);

  await fetch(API+"/message",{method:"POST",body:fd});
  loadMsgs();
}

async function loadMsgs(){
  const r = await fetch(API+"/messages");
  const d = await r.json();

  let html = "";
  d.forEach(m=>{
    html += `<li>${m.texto}</li>`;
  });

  document.getElementById("msgList").innerHTML = html;
}

// IMAGEM
async function uploadImg(){
  const file = document.getElementById("img").files[0];

  const fd = new FormData();
  fd.append("file", file);

  const r = await fetch(API+"/upload-image",{method:"POST",body:fd});
  const d = await r.json();

  document.getElementById("preview").src = URL.createObjectURL(file);
}

// DISPARO
async function send(){
  await fetch(API+"/send",{method:"POST"});
  loadLeads();
}

// INIT
loadStatus();
loadLeads();
loadMsgs();
setInterval(loadStatus, 5000);