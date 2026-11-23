using System.Globalization;
using System.Windows;
using System.Windows.Data;

namespace SciDev.Desktop;

public sealed class DocumentSelectionConverter : IMultiValueConverter
{
    public object Convert(object[] values, Type targetType, object parameter, CultureInfo culture) =>
        values.Length == 2 && values[0] is string path && values[1] is string active &&
        string.Equals(path.Replace('\\', '/'), active.Replace('\\', '/'), StringComparison.OrdinalIgnoreCase);

    public object[] ConvertBack(object value, Type[] targetTypes, object parameter, CultureInfo culture) =>
        targetTypes.Select(_ => Binding.DoNothing).ToArray();
}
