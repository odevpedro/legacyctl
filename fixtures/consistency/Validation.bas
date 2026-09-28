Attribute VB_Name = "Validation"
Option Explicit

' ---------------------------------------------------------------------------
' Validation.bas (fixture de consistencia)
' Hub transversal de validacao compartilhado entre os fluxos.
' ---------------------------------------------------------------------------

Public Function IsBlank(ByVal value As Variant) As Boolean
    IsBlank = IsNull(value) Or Len(Trim$(CStr(value))) = 0
End Function
