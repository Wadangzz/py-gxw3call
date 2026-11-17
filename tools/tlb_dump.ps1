param([string]$Path)
$src = @'
using System;
using System.Text;
using System.Runtime.InteropServices;
using CT = System.Runtime.InteropServices.ComTypes;

public static class TlbDump {
  [DllImport("oleaut32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
  static extern void LoadTypeLibEx(string file, int regkind, out CT.ITypeLib tlb);

  static string VtName(short vt) {
    switch (vt & 0xFFF) {
      case 2: return "short"; case 3: return "long"; case 4: return "float";
      case 5: return "double"; case 8: return "BSTR"; case 9: return "IDispatch*";
      case 11: return "VARIANT_BOOL"; case 12: return "VARIANT"; case 13: return "IUnknown*";
      case 16: return "char"; case 17: return "byte"; case 18: return "ushort";
      case 19: return "ulong"; case 20: return "int64"; case 21: return "uint64";
      case 22: return "int"; case 23: return "uint"; case 24: return "void";
      case 25: return "HRESULT"; case 27: return "SAFEARRAY"; case 30: return "LPSTR"; case 31: return "LPWSTR";
      default: return "vt" + (vt & 0xFFF);
    }
  }

  static string TypeName(CT.ITypeInfo ti, CT.TYPEDESC td) {
    short vt = td.vt;
    if (vt == 26) { // PTR
      var inner = (CT.TYPEDESC)Marshal.PtrToStructure(td.lpValue, typeof(CT.TYPEDESC));
      return TypeName(ti, inner) + "*";
    }
    if (vt == 27) { // SAFEARRAY
      var inner = (CT.TYPEDESC)Marshal.PtrToStructure(td.lpValue, typeof(CT.TYPEDESC));
      return "SAFEARRAY(" + TypeName(ti, inner) + ")";
    }
    if (vt == 28) { // CARRAY
      var inner = (CT.TYPEDESC)Marshal.PtrToStructure(td.lpValue, typeof(CT.TYPEDESC));
      int off = Marshal.SizeOf(typeof(CT.TYPEDESC));
      return TypeName(ti, inner) + "[" + Marshal.ReadInt32(td.lpValue, off + 4) + "]";
    }
    if (vt == 29) { // USERDEFINED
      try {
        CT.ITypeInfo rt; ti.GetRefTypeInfo((int)(td.lpValue.ToInt64() & 0xFFFFFFFF), out rt);
        string n, d, h; int c; rt.GetDocumentation(-1, out n, out d, out c, out h);
        return n;
      } catch { return "userdef"; }
    }
    return VtName(vt);
  }

  public static string Dump(string path) {
    var sb = new StringBuilder();
    CT.ITypeLib tlb; LoadTypeLibEx(path, 2, out tlb);
    string ln, ld, lh; int lc; tlb.GetDocumentation(-1, out ln, out ld, out lc, out lh);
    IntPtr pla; tlb.GetLibAttr(out pla);
    var la = (CT.TYPELIBATTR)Marshal.PtrToStructure(pla, typeof(CT.TYPELIBATTR));
    sb.AppendLine("LIB " + ln + " " + la.guid + " v" + la.wMajorVerNum + "." + la.wMinorVerNum + " : " + ld);
    int n = tlb.GetTypeInfoCount();
    for (int i = 0; i < n; i++) {
      CT.ITypeInfo ti; tlb.GetTypeInfo(i, out ti);
      string name, doc, hf; int hc; ti.GetDocumentation(-1, out name, out doc, out hc, out hf);
      IntPtr pta; ti.GetTypeAttr(out pta);
      var ta = (CT.TYPEATTR)Marshal.PtrToStructure(pta, typeof(CT.TYPEATTR));
      sb.AppendLine();
      sb.AppendLine(ta.typekind + " " + name + " " + ta.guid + " funcs=" + ta.cFuncs + " vars=" + ta.cVars + (doc != null ? " // " + doc : ""));
      for (int f = 0; f < ta.cFuncs; f++) {
        IntPtr pfd; ti.GetFuncDesc(f, out pfd);
        var fd = (CT.FUNCDESC)Marshal.PtrToStructure(pfd, typeof(CT.FUNCDESC));
        var names = new string[fd.cParams + 1]; int got;
        ti.GetNames(fd.memid, names, names.Length, out got);
        var ps = new StringBuilder();
        for (int p = 0; p < fd.cParams; p++) {
          var ed = (CT.ELEMDESC)Marshal.PtrToStructure(new IntPtr(fd.lprgelemdescParam.ToInt64() + p * Marshal.SizeOf(typeof(CT.ELEMDESC))), typeof(CT.ELEMDESC));
          int fl = (int)ed.desc.paramdesc.wParamFlags;
          string dir = ((fl & 1) != 0 ? "in" : "") + ((fl & 2) != 0 ? "out" : "") + ((fl & 8) != 0 ? ",retval" : "");
          if (p > 0) ps.Append(", ");
          ps.Append("[" + dir + "] " + TypeName(ti, ed.tdesc) + " " + (p + 1 < got ? names[p + 1] : "?"));
        }
        sb.AppendLine("  " + fd.invkind.ToString().Replace("INVOKE_", "") + " " + TypeName(ti, fd.elemdescFunc.tdesc) + " " + names[0] + "(" + ps + ")");
        ti.ReleaseFuncDesc(pfd);
      }
      if (ta.typekind == CT.TYPEKIND.TKIND_ENUM || ta.typekind == CT.TYPEKIND.TKIND_RECORD) {
        for (int v = 0; v < ta.cVars; v++) {
          IntPtr pvd; ti.GetVarDesc(v, out pvd);
          var vd = (CT.VARDESC)Marshal.PtrToStructure(pvd, typeof(CT.VARDESC));
          var vn = new string[1]; int g; ti.GetNames(vd.memid, vn, 1, out g);
          string val = "";
          if (ta.typekind == CT.TYPEKIND.TKIND_ENUM) {
            try { val = " = " + Marshal.GetObjectForNativeVariant(vd.desc.lpvarValue); } catch { }
          } else {
            val = " : " + TypeName(ti, vd.elemdescVar.tdesc);
          }
          sb.AppendLine("  " + vn[0] + val);
          ti.ReleaseVarDesc(pvd);
        }
      }
      ti.ReleaseTypeAttr(pta);
    }
    return sb.ToString();
  }
}
'@
Add-Type -TypeDefinition $src
[TlbDump]::Dump($Path)
